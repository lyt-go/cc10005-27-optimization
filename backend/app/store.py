"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS

# 看板模块顺序：与前端导航保持一致，去重索引重建紧跟采集去重。
MODULE_ORDER = [
    "connector", "proxypool", "deepcrawl", "jsrender", "parser", "dataclean",
    "formatter", "dedupengine", "deduprebuild", "throttle", "captcha", "monitor",
    "exporttask", "seedmgr", "sitemap", "cookiejar", "healthcheck", "snapshot",
    "audittrail",
]
MODULE_LABELS = {
    "connector": "连接器管理",
    "proxypool": "代理池管理",
    "deepcrawl": "深度爬取",
    "jsrender": "JS渲染采集",
    "parser": "字段解析器",
    "dataclean": "数据去噪",
    "formatter": "格式转换",
    "dedupengine": "采集去重",
    "deduprebuild": "去重索引重建",
    "throttle": "动态限速",
    "captcha": "验证码处理",
    "monitor": "采集监控",
    "exporttask": "数据分发",
    "seedmgr": "种子管理",
    "sitemap": "站点地图",
    "cookiejar": "Cookie管理",
    "healthcheck": "采集器健康",
    "snapshot": "采集快照",
    "audittrail": "操作审计",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        # 看板按导航相同顺序排列；未登记顺序的模块排在最后。
        ordered = [name for name in MODULE_ORDER if name in self._tables]
        ordered += sorted(name for name in self._tables if name not in MODULE_ORDER)
        modules: list[dict[str, object]] = []
        for name in ordered:
            rows = self.rows(name)
            item: dict[str, object] = {
                "name": name,
                "label": MODULE_LABELS.get(name, name),
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            }
            if name == "deduprebuild":
                # 概览中同步呈现去重结果：受影响批次、复用窗口与转历史批次。
                item["dedup"] = {
                    "受影响批次": sum(1 for row in rows if row.get("影响判定") == "受影响"),
                    "复用窗口": sum(int(row.get("复用窗口") or 0) for row in rows),
                    "转历史批次": sum(1 for row in rows if row.get("status") == "已转历史"),
                }
            modules.append(item)
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
