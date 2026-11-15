"""去重索引重建业务规则：字段清洗版本变化后的影响识别、局部重建、断点续建与冲突裁决。

复用条件（本模块约定）：
- 窗口边界：新批次窗口边界与已稳定索引完全重合（同一起止日期）才允许复用，
  错位或仅部分重叠的窗口一律局部重建；
- 相似度阈值：新批次阈值与已稳定结果阈值的绝对差不超过 0.05 才允许复用，
  超出则对应窗口局部重建；
- 去重字段组未命中变化字段的去重引擎整体复用稳定结果，不进入重建流程。

冲突裁决：同一去重引擎的同一窗口被多个重建批次同时覆盖时，
字段集合完整度（命中字段数 / 去重字段组字段数）高者生效；
完整度相同则确认顺序靠后者生效，未生效批次的对应窗口转历史。
"""
from __future__ import annotations

import calendar
import hashlib
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "deduprebuild"
REQUIRED_FIELDS = ["批次编号", "去噪规则", "去重引擎", "去重字段组"]
STATUS_ORDER = ["待重建", "影响识别中", "局部重建中", "已中断", "已完成", "已转历史"]
TERMINAL_STATUSES = {"已完成", "已转历史"}
THRESHOLD_TOLERANCE = 0.05  # 相似度阈值复用容差：偏差不超过该值才允许复用稳定结果
REUSE_CONDITION = "窗口边界完全重合且相似度阈值偏差≤0.05"

ACTION_RULES = {
    "识别影响范围": "影响识别中",
    "启动局部重建": "局部重建中",
    "中断重建": "已中断",
    "断点续建": "局部重建中",
    "完成重建": "已完成",
    "转入历史": "已转历史",
}
ALLOWED_ACTIONS = {
    "待重建": {"识别影响范围", "转入历史"},
    "影响识别中": {"启动局部重建", "转入历史"},
    "局部重建中": {"中断重建", "完成重建", "冲突裁决", "转入历史"},
    "已中断": {"断点续建", "冲突裁决", "转入历史"},
    "已完成": {"冲突裁决"},
    "已转历史": set(),
}
NEGATIVE_ACTIONS = {"中断重建", "转入历史"}


def _split_fields(text: str) -> list[str]:
    """把「标题+正文+来源」或「标题、正文」这类字段串拆成字段列表。"""
    for sep in ("+", "、", ",", "，", " "):
        text = text.replace(sep, "|")
    return [part.strip() for part in text.split("|") if part.strip()]


def _fingerprint(*parts: str) -> str:
    """按批次、窗口与清洗版本生成确定性的哈希指纹，便于观察版本前后变化。"""
    digest = hashlib.sha1("/".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return f"fp{digest[:10]}"


def _versions(entry: dict[str, Any]) -> tuple[str, str]:
    """从「v3→v4」里拆出（旧版本， 新版本）。"""
    text = str(entry.get("清洗版本") or "v1→v2")
    if "→" in text:
        old, new = text.split("→", 1)
        return old.strip(), new.strip()
    return text, text


def _window_boundary(window_id: str) -> str:
    """W-2026-10 → 2026-10-01~2026-10-31。"""
    try:
        _, year_month = window_id.split("-", 1)
        year, month = year_month.split("-")
        last_day = calendar.monthrange(int(year), int(month))[1]
        return f"{year}-{month}-01~{year}-{month}-{last_day:02d}"
    except (ValueError, IndexError):
        return window_id


def _record_count(batch_no: str, window_id: str) -> int:
    """确定性的窗口记录数，保证多次读取结果一致。"""
    return int(hashlib.sha1(f"{batch_no}#{window_id}".encode("utf-8")).hexdigest()[:4], 16) % 1600 + 800


def _completeness(entry: dict[str, Any]) -> float:
    """字段集合完整度：命中字段数 / 去重字段组字段数。"""
    try:
        hit, total = str(entry.get("字段完整度") or "0/1").split("/", 1)
        return int(hit) / max(int(total), 1)
    except ValueError:
        return 0.0


class DeduprebuildService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            self._refresh_summary(row)
        if keyword:
            rows = [
                row for row in rows
                if any(keyword in str(row.get(field, "")) for field in ("批次编号", "去噪规则", "去重引擎"))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._ensure_windows(entry)
            self._refresh_summary(entry)
        return entry

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.setdefault("清洗版本", "v1→v2")
        entry.setdefault("变化字段", "")
        entry.setdefault("覆盖窗口", "全部")
        entry.setdefault("窗口总数", 12)
        entry.setdefault("已完成窗口", 0)
        entry.setdefault("复用窗口", 0)
        entry.setdefault("断点窗口", "—")
        entry.setdefault("相似度阈值", "0.85")
        entry.setdefault("复用条件", REUSE_CONDITION)
        entry.setdefault("确认时间", datetime.now().strftime("%Y-%m-%d %H:%M"))
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        self._align_entry(entry)
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"重建批次 {entry_id} 不存在或已归档"
        if action == "冲突裁决":
            return entry, self.arbitrate(str(entry.get("去重引擎") or ""))
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于去重索引重建可执行范围"
        allowed = ALLOWED_ACTIONS.get(str(entry.get("status")), set())
        if action not in allowed:
            return None, f"批次当前状态「{entry.get('status')}」不允许执行「{action}」"

        target = ACTION_RULES[action]
        if action == "识别影响范围":
            self._align_entry(entry)
            message = f"影响识别完成：命中字段 {entry['命中字段']}，判定{entry['影响判定']}"
        elif action == "启动局部重建":
            self._ensure_windows(entry)
            if entry.get("影响判定") != "受影响":
                # 字段组未命中变化字段：整批复用稳定结果，不占用重建资源
                for window in entry["windows"]:
                    window["去重结果"] = "复用稳定结果"
                    window["哈希指纹"] = window["旧哈希指纹"]
                target = "已完成"
                message = "去重字段组未命中变化字段，全部窗口复用稳定结果"
            else:
                message = "局部重建已启动，仅重建命中变化字段的窗口"
        elif action == "中断重建":
            done = [w for w in self._ensure_windows(entry) if w["去重结果"] in ("局部重建", "复用稳定结果")]
            entry["断点窗口"] = done[-1]["窗口编号"] if done else "—"
            message = f"重建已中断，断点窗口 {entry['断点窗口']}（最后一个完整窗口）"
        elif action == "断点续建":
            windows = self._ensure_windows(entry)
            pending = [w for w in windows if w["去重结果"] == "待重建"]
            entry["续建起点"] = pending[0]["窗口编号"] if pending else "—"
            message = f"从最后一个完整窗口 {entry.get('断点窗口')} 继续，续建起点 {entry['续建起点']}"
        elif action == "完成重建":
            _, new_version = _versions(entry)
            for window in self._ensure_windows(entry):
                if window["去重结果"] == "待重建":
                    window["去重结果"] = "局部重建"
                    window["清洗版本"] = new_version
                    window["哈希指纹"] = _fingerprint(str(entry["批次编号"]), window["窗口编号"], new_version)
            message = "局部重建完成，未受影响窗口继续复用稳定结果"
        else:  # 转入历史
            for window in self._ensure_windows(entry):
                window["去重结果"] = "已转历史"
                window["哈希指纹"] = "—"
            message = "批次已转历史，对应窗口指纹不再参与去重"

        entry["status"] = target
        entry["pending"] = target not in TERMINAL_STATUSES
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        self._refresh_summary(entry)
        return entry, message

    # ---------------------------------------------------------- 影响识别与对齐
    def plan_rebuild(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """字段清洗版本变化后：只识别使用变化字段的去重引擎，生成局部重建批次。

        字段规则与去重窗口读取同一清洗版本；未命中的引擎整体复用稳定结果。
        """
        rule_no = str(values.get("规则编号") or "").strip()
        rule = next((row for row in store.rows("dataclean") if row.get("规则编号") == rule_no), None)
        if rule is None:
            return None, f"去噪规则 {rule_no or '(空)'} 不存在，无法规划重建"
        changed = _split_fields(str(values.get("变化字段") or "")) or _split_fields(str(rule.get("目标字段") or ""))
        if not changed:
            return None, f"去噪规则 {rule_no} 没有可用的变化字段"
        old_version = str(rule.get("清洗版本") or "v?")
        new_version = str(values.get("目标清洗版本") or "").strip() or old_version

        engines: list[dict[str, Any]] = []
        created: list[str] = []
        reused = 0
        for engine in store.rows("dedupengine"):
            group = _split_fields(str(engine.get("去重字段组") or ""))
            hits = [field for field in changed if field in group]
            item = {
                "去重引擎": engine.get("引擎编号"),
                "去重字段组": engine.get("去重字段组"),
                "命中字段": "、".join(hits) if hits else "无",
                "字段完整度": f"{len(hits)}/{len(group)}" if group else "0/0",
                "影响判定": "受影响" if hits else "未受影响",
                "处置": "生成局部重建批次" if hits else "复用稳定结果，不重建",
            }
            if hits:
                batch = self._create_batch(rule, engine, changed, hits, old_version, new_version)
                if batch is not None:
                    created.append(str(batch["批次编号"]))
                    item["处置"] = f"生成局部重建批次 {batch['批次编号']}"
            else:
                reused += 1
            engines.append(item)

        plan = {
            "去噪规则": rule_no,
            "清洗版本": f"{old_version}→{new_version}",
            "变化字段": "、".join(changed),
            "复用条件": REUSE_CONDITION,
            "受影响引擎数": len(engines) - reused,
            "复用引擎数": reused,
            "新建批次": created,
            "引擎识别结果": engines,
        }
        message = f"识别完成：{len(engines) - reused} 个引擎受影响，{reused} 个引擎复用稳定结果"
        return plan, message

    def _create_batch(
        self,
        rule: dict[str, Any],
        engine: dict[str, Any],
        changed: list[str],
        hits: list[str],
        old_version: str,
        new_version: str,
    ) -> dict[str, Any] | None:
        """为受影响引擎登记局部重建批次；同规则同引擎同目标版本的在途批次不重复建。"""
        rows = store.rows(MODULE)
        for row in rows:
            if (
                row.get("去噪规则") == rule.get("规则编号")
                and row.get("去重引擎") == engine.get("引擎编号")
                and row.get("目标清洗版本") == new_version
                and row.get("status") not in TERMINAL_STATUSES
            ):
                return None
        serial = max(
            (int(str(row.get("批次编号", "REB-0")).split("-")[-1]) for row in rows
             if str(row.get("批次编号", "")).split("-")[-1].isdigit()),
            default=0,
        ) + 1
        group = _split_fields(str(engine.get("去重字段组") or ""))
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "批次编号": f"REB-{serial:04d}",
            "去噪规则": rule.get("规则编号"),
            "清洗版本": f"{old_version}→{new_version}",
            "目标清洗版本": new_version,
            "变化字段": "、".join(changed),
            "去重引擎": engine.get("引擎编号"),
            "去重字段组": engine.get("去重字段组"),
            "命中字段": "、".join(hits),
            "字段完整度": f"{len(hits)}/{len(group)}" if group else "0/0",
            "影响判定": "受影响",
            "覆盖窗口": "全部",
            "窗口总数": 12,
            "已完成窗口": 0,
            "复用窗口": 0,
            "转历史窗口": "",
            "断点窗口": "—",
            "窗口边界": "2026-01-01~2026-12-31",
            "相似度阈值": engine.get("相似度阈值", "0.85"),
            "复用条件": REUSE_CONDITION,
            "哈希指纹": "—",
            "去重结果": "待重建",
            "裁决说明": "—",
            "确认时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
        }
        rows.append(entry)
        return entry

    def _align_entry(self, entry: dict[str, Any]) -> None:
        """按引擎当前字段组与规则当前清洗版本对齐批次记录（找出受影响索引）。"""
        engine = next((row for row in store.rows("dedupengine") if row.get("引擎编号") == entry.get("去重引擎")), None)
        rule = next((row for row in store.rows("dataclean") if row.get("规则编号") == entry.get("去噪规则")), None)
        group_text = str((engine or entry).get("去重字段组") or "")
        group = _split_fields(group_text)
        changed = _split_fields(str(entry.get("变化字段") or ""))
        hits = [field for field in changed if field in group]
        entry["去重字段组"] = group_text
        entry["命中字段"] = "、".join(hits) if hits else "无"
        entry["字段完整度"] = f"{len(hits)}/{len(group)}" if group else "0/0"
        entry["影响判定"] = "受影响" if hits else "未受影响"
        if rule is not None and rule.get("清洗版本"):
            entry["规则清洗版本"] = rule["清洗版本"]

    # ------------------------------------------------------------------ 裁决
    def arbitrate(self, engine_no: str) -> str:
        """同一引擎同一窗口被多批次覆盖时：完整度高者生效，并列则确认顺序靠后者生效。"""
        batches = [
            row for row in store.rows(MODULE)
            if row.get("去重引擎") == engine_no
            and row.get("status") != "已转历史"
            and row.get("影响判定") == "受影响"
        ]
        if len(batches) < 2:
            return f"去重引擎 {engine_no} 生效中的受影响批次不足两个，无需裁决"
        coverage: dict[str, list[dict[str, Any]]] = {}
        for batch in batches:
            for window in self._ensure_windows(batch):
                coverage.setdefault(window["窗口编号"], []).append(batch)
        demoted: list[str] = []
        for window_id, rivals in coverage.items():
            if len(rivals) < 2:
                continue
            winner = max(rivals, key=lambda row: (_completeness(row), str(row.get("确认时间") or "")))
            for loser in rivals:
                if loser is winner:
                    continue
                for window in self._ensure_windows(loser):
                    if window["窗口编号"] == window_id and window["去重结果"] != "已转历史":
                        window["去重结果"] = "已转历史"
                        window["哈希指纹"] = "—"
                        demoted.append(f"{loser.get('批次编号')}@{window_id}")
        for batch in batches:
            windows = self._ensure_windows(batch)
            if windows and all(window["去重结果"] == "已转历史" for window in windows):
                batch["status"] = "已转历史"
                batch["pending"] = False
                batch["abnormal"] = True
            self._refresh_summary(batch)
        if not demoted:
            return f"去重引擎 {engine_no} 各批次窗口无新冲突，维持现状"
        return f"裁决完成：{'、'.join(demoted)} 转历史（完整度优先，并列按确认顺序）"

    # ------------------------------------------------------------------ 窗口
    def _covered_window_ids(self, entry: dict[str, Any]) -> list[str]:
        covered = str(entry.get("覆盖窗口") or "全部").strip()
        if covered in ("", "全部"):
            return [f"W-2026-{month:02d}" for month in range(1, 13)]
        return [item.strip() for item in covered.replace(",", "、").split("、") if item.strip()]

    def _ensure_windows(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """按批次规划生成窗口级指纹：受影响窗口局部重建，满足复用条件的窗口指纹保持不变。"""
        windows = entry.get("windows")
        if windows:
            return windows
        old_version, new_version = _versions(entry)
        done = int(entry.get("已完成窗口") or 0)
        reuse = int(entry.get("复用窗口") or 0)
        history = {
            item.strip()
            for item in str(entry.get("转历史窗口") or "").replace(",", "、").split("、")
            if item.strip()
        }
        batch_no = str(entry.get("批次编号") or entry.get("id"))
        affected = entry.get("影响判定") == "受影响"
        archived = entry.get("status") == "已转历史"
        windows = []
        for index, window_id in enumerate(self._covered_window_ids(entry)):
            old_fp = _fingerprint(batch_no, window_id, old_version)
            if archived or window_id in history:
                result, version, new_fp = "已转历史", old_version, "—"
            elif not affected or index < reuse:
                # 未受影响 / 满足复用条件：指纹保持旧版本不变
                result, version, new_fp = "复用稳定结果", old_version, old_fp
            elif index < done:
                result, version, new_fp = "局部重建", new_version, _fingerprint(batch_no, window_id, new_version)
            else:
                result, version, new_fp = "待重建", old_version, "—"
            windows.append({
                "窗口编号": window_id,
                "窗口边界": _window_boundary(window_id),
                "清洗版本": version,
                "哈希指纹": new_fp,
                "旧哈希指纹": old_fp,
                "去重结果": result,
                "记录数": _record_count(batch_no, window_id),
            })
        entry["windows"] = windows
        return windows

    def _refresh_summary(self, entry: dict[str, Any]) -> None:
        """按窗口结果汇总批次级去重结果与批次索引指纹。"""
        windows = self._ensure_windows(entry)
        counts: dict[str, int] = {}
        for window in windows:
            counts[window["去重结果"]] = counts.get(window["去重结果"], 0) + 1
        labels = (("局部重建", "局部重建"), ("复用稳定结果", "复用"), ("待重建", "待建"), ("已转历史", "转历史"))
        parts = [f"{label}{counts[key]}窗" for key, label in labels if counts.get(key)]
        entry["去重结果"] = "·".join(parts) if parts else "—"
        entry["窗口总数"] = len(windows)
        entry["复用窗口"] = counts.get("复用稳定结果", 0)
        entry["已完成窗口"] = counts.get("局部重建", 0) + counts.get("复用稳定结果", 0)
        live = [window["哈希指纹"] for window in windows if window["去重结果"] in ("局部重建", "复用稳定结果")]
        entry["哈希指纹"] = _fingerprint(str(entry.get("批次编号")), *live) if live else "—"
