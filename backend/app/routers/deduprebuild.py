"""去重索引重建接口：字段清洗版本变化后的影响识别、局部重建批次管理与冲突裁决。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.deduprebuild import DeduprebuildService

router = APIRouter(prefix="/api/deduprebuild", tags=["去重索引重建"])

service = DeduprebuildService()

LIST_FIELDS = ["批次编号", "去噪规则", "去重引擎", "去重字段组", "变化字段", "影响判定", "断点窗口", "去重结果"]
STATUSES = ["待重建", "影响识别中", "局部重建中", "已中断", "已完成", "已转历史"]


def _strip_windows(entry: dict[str, Any]) -> dict[str, Any]:
    """列表与导出不带窗口级明细，窗口指纹走详情接口读取。"""
    return {key: value for key, value in entry.items() if key != "windows"}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按批次编号、去噪规则或去重引擎检索"),
    status: str | None = Query(default=None, description="待重建、影响识别中、局部重建中、已中断、已完成、已转历史"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按批次编号、去噪规则、去重引擎与状态过滤重建批次；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=[_strip_windows(item) for item in items], total=total, page=page, size=size)


@router.post("/plan")
def plan_rebuild(payload: EntryPayload) -> dict[str, Any]:
    """字段清洗版本变化后规划重建：只识别使用变化字段的去重引擎并生成局部重建批次。

    未命中变化字段的引擎整体复用稳定结果，不生成批次、不刷新既有去重结果。
    """
    plan, message = service.plan_rebuild(payload.values)
    if plan is None:
        return {"ok": False, "message": message, "plan": None}
    return {"ok": True, "message": message, "plan": plan}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出去重索引重建清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "deduprebuild", "total": total, "items": [_strip_windows(item) for item in items]}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条重建批次明细（含窗口级哈希指纹与去重结果）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重建批次 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条重建批次，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="重建批次已登记", entry=_strip_windows(entry))


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条重建批次执行识别、重建、中断、续建、裁决等动作；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=_strip_windows(entry))
