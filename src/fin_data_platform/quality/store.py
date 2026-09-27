"""质量结果读写（``meta.quality_results``）：明细写入 + 报告查询。

写入为「先清同 ``run_id`` 旧行、再追加」——任务重试 / 重跑幂等；
查询供管理 API / WebUI「质量」页（按日 × 数据集聚合 + 明细下钻）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import Engine, func, select

from fin_data_platform.quality.models import CheckResult
from fin_data_platform.quality.schema import quality_results
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.storage.writers import append_rows

_TABLE = quality_results


def write_results(engine: Engine, *, run_id: int, results: Sequence[CheckResult]) -> int:
    """写入一次扫描的全部结果；返回实际写入行数。"""
    now = utcnow()
    rows = [
        {
            "run_id": run_id,
            "dataset": item.dataset,
            "check_id": item.check_id,
            "family": item.family,
            "severity": item.severity,
            "status": item.status,
            "window_start": item.window_start,
            "window_end": item.window_end,
            "rows_checked": item.rows_checked,
            "violations": item.violations,
            "samples": json.dumps(list(item.samples), ensure_ascii=False, default=str),
            "metrics": json.dumps(item.metrics, ensure_ascii=False, default=str),
            "message": item.message,
            "created_at": now,
        }
        for item in results
    ]
    with engine.begin() as connection:
        connection.execute(_TABLE.delete().where(_TABLE.c.run_id == run_id))
        return append_rows(connection, _TABLE, rows)


def latest_day(engine: Engine) -> date | None:
    """最近一次扫描的报告日（``window_end`` 最大值）。"""
    with engine.connect() as connection:
        return connection.execute(select(func.max(_TABLE.c.window_end))).scalar_one_or_none()


def fetch_results(
    engine: Engine,
    *,
    day: date | None = None,
    dataset: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """明细查询（``day`` 缺省 = 最近报告日；同日多次运行以**最新一次**为准）。"""
    conditions = []
    with engine.connect() as connection:
        if day is None:
            day = connection.execute(select(func.max(_TABLE.c.window_end))).scalar_one_or_none()
        if day is not None:
            conditions.append(_TABLE.c.window_end == day)
        run_id = connection.execute(
            select(func.max(_TABLE.c.run_id)).where(*conditions)
        ).scalar_one_or_none()
        if run_id is not None:
            conditions.append(_TABLE.c.run_id == run_id)
        if dataset:
            conditions.append(_TABLE.c.dataset == dataset)
        if status:
            conditions.append(_TABLE.c.status == status)
        if severity:
            conditions.append(_TABLE.c.severity == severity)
        total = connection.execute(
            select(func.count()).select_from(_TABLE).where(*conditions)
        ).scalar_one()
        rows = connection.execute(
            select(_TABLE)
            .where(*conditions)
            .order_by(_TABLE.c.dataset, _TABLE.c.check_id)
            .limit(limit)
            .offset(offset)
        ).mappings()
        items = [_row_dict(row) for row in rows]
    return int(total), items


def fetch_summary(engine: Engine, *, day: date | None = None) -> dict[str, Any]:
    """按日 × 数据集的报告摘要（供「质量」页首屏；同日多次运行以**最新一次**为准）。"""
    conditions = []
    with engine.connect() as connection:
        if day is None:
            day = connection.execute(select(func.max(_TABLE.c.window_end))).scalar_one_or_none()
        if day is None:
            return {"day": None, "generated_at": None, "datasets": []}
        conditions.append(_TABLE.c.window_end == day)
        run_id = connection.execute(
            select(func.max(_TABLE.c.run_id)).where(*conditions)
        ).scalar_one_or_none()
        if run_id is not None:
            conditions.append(_TABLE.c.run_id == run_id)
        rows = [
            _row_dict(row)
            for row in connection.execute(select(_TABLE).where(*conditions)).mappings()
        ]
    datasets: dict[str, dict[str, Any]] = {}
    generated: Any = None
    for item in rows:
        bucket = datasets.setdefault(
            item["dataset"],
            {
                "dataset": item["dataset"],
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "error": 0,
                "warnings": 0,
                "failed_checks": [],
                "violations": 0,
            },
        )
        bucket["total"] += 1
        bucket[item["status"]] = bucket.get(item["status"], 0) + 1
        if item["severity"] == "warn" and item["status"] in ("failed", "error"):
            bucket["warnings"] += 1
        bucket["violations"] += int(item["violations"] or 0)
        if item["status"] == "failed":
            bucket["failed_checks"].append(item["check_id"])
        metrics = item["metrics"] or {}
        if item["check_id"] == "completeness" and "ratio" in metrics:
            bucket["coverage_ratio"] = metrics["ratio"]
        if item["check_id"] == "freshness" and "lag_days" in metrics:
            bucket["freshness_lag_days"] = metrics["lag_days"]
        if item["created_at"] is not None and (
            generated is None or item["created_at"] > generated
        ):
            generated = item["created_at"]
    ordered = sorted(datasets.values(), key=lambda item: item["dataset"])
    return {
        "day": day.isoformat() if day else None,
        "generated_at": generated.isoformat() if generated is not None else None,
        "datasets": ordered,
    }


def _row_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["samples"] = json.loads(item["samples"]) if item.get("samples") else []
    item["metrics"] = json.loads(item["metrics"]) if item.get("metrics") else {}
    return item
