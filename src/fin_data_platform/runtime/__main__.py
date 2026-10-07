"""FinDataRuntime 入口：``python -m fin_data_platform.runtime --role all|scheduler|worker``。

角色可拆进程（doc-20 §3.2）：``scheduler`` 只做调度与分发，``worker`` 只做执行，
两者仅凭 PostgreSQL 协调；``all`` 为单机默认。

同步任务由环境变量装配（TASK-3.6 切片 3）：
``FDP_SYNC_CODES`` / ``FDP_SYNC_START`` / ``FDP_SYNC_SOURCE`` / ``FDP_SYNC_SCHEDULE``；
未配置时启动为空 Runtime（仅控制面）。

派生任务（TASK-3.12）：字典 ``materialize=latest`` 的派生自动注册（``derive.*``）；
``refresh=scheduled`` 使用 ``FDP_DERIVE_SCHEDULE``（cron / interval:<秒>），未配置则仅手动触发。

全市场基础信息与生命周期（TASK-3.35）：``FDP_REGISTRY_SOURCE``（缺省取
``FDP_SYNC_SOURCE``）指定来源，注册全局任务 ``sync.reference.market_registry``
（启动即首灌；``FDP_REGISTRY_SCHEDULE`` 配置后定期刷新；可由管理界面触发）。

质量扫描（TASK-3.5）：注册全局任务 ``quality.scan``（规则 / 完整性 / 时效性 /
引用与跨源对账）；``FDP_QUALITY_SCHEDULE`` 配置后定期执行（缺省仅手动 / 管理界面
触发），``FDP_QUALITY_DATASETS`` / ``FDP_QUALITY_CODES`` / ``FDP_QUALITY_LOOKBACK_DAYS`` /
``FDP_QUALITY_RECONCILE_CODES`` 分别配置扫描数据集、期望范围、回看交易日与对账样本。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading

from fin_data_platform.cache import cache_from_env
from fin_data_platform.derived.store import SqlAlgorithmStore
from fin_data_platform.derived.sync import sync_algorithms
from fin_data_platform.derived.tasks import register_derived_tasks
from fin_data_platform.export import (
    DEFAULT_CHUNK_DAYS,
    DEFAULT_ENTITY_BATCH,
    reconcile_stale_exports,
    register_export_task,
)
from fin_data_platform.ingestion import build_hub, register_market_registry_task
from fin_data_platform.ingestion.bootstrap import build_sync_runtime, supports_capability
from fin_data_platform.ingestion.settings import SyncSettings
from fin_data_platform.quality import DEFAULT_DATASETS, register_quality_task
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.app import RuntimeApp
from fin_data_platform.runtime.config import ROLES, RuntimeConfig
from fin_data_platform.runtime.registry import TaskRegistry, TaskSpec
from fin_data_platform.storage.engine import create_write_engine

logger = logging.getLogger("fin_data_platform.runtime")


def _split_env(name: str) -> tuple[str, ...]:
    """逗号分隔环境变量 → 去空白元组（空值返回空元组）。"""
    raw = os.environ.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _int_env(name: str, default: int) -> int:
    """整数环境变量（缺省/非法时回退默认值并告警）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r 非法，使用缺省值 %d", name, raw, default)
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fin-data-platform-runtime",
        description="FinDataRuntime（控制面进程；doc-20）",
    )
    parser.add_argument("--role", choices=ROLES, default="all", help="进程角色")
    parser.add_argument("--workers", type=int, default=2, help="WorkerPool 线程数")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅执行就绪检查后退出（0 通过 / 1 未通过；供容器健康检查）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        settings = SyncSettings.from_env()
    except ValueError as exc:
        logger.error("同步配置非法: %s", exc)
        return 2

    config = RuntimeConfig.from_env(role=args.role, worker_count=args.workers)
    engine = create_write_engine(config.storage)
    registry = TaskRegistry()
    active_cache = cache_from_env()

    # 全市场登记（TASK-3.35）：独立于按代码同步；来源可单独配置（缺省取同步源）
    registry_source = (os.environ.get("FDP_REGISTRY_SOURCE") or "").strip() or (
        settings.source if settings is not None else ""
    )
    registry_schedule = (os.environ.get("FDP_REGISTRY_SCHEDULE") or "").strip() or None
    hub = None
    if registry_source:
        try:
            hub = build_hub(os.environ)
        except Exception as exc:  # 凭证缺失等：不阻塞进程启动
            logger.warning("全市场登记任务未装配（hub 构建失败）：%s", exc)

    if settings is not None:
        app = build_sync_runtime(
            config, settings, engine=engine, registry=registry, cache=active_cache, hub=hub
        )
        logger.info(
            "同步任务装配：codes=%s source=%s schedule=%s start=%s",
            ",".join(settings.codes),
            settings.source or "auto",
            settings.schedule or "manual",
            settings.start.isoformat(),
        )
    else:
        app = RuntimeApp(config, engine=engine, registry=registry)
    report = app.readiness()
    if report is not None and not report.ok:
        logger.error("readiness 未通过: %s", "; ".join(report.errors))
        return 1
    if args.check:
        logger.info("就绪检查通过（--check）")
        return 0

    # 派生任务装配（TASK-3.12）：字典/算法一致性失败即拒绝启动；
    # 登记同步幂等（历史 id 只置 deprecated，不删除）
    algorithm_store = SqlAlgorithmStore(engine)
    derived = register_derived_tasks(
        registry,
        engine,
        store=algorithm_store,
        schedule=os.environ.get("FDP_DERIVE_SCHEDULE") or None,
        cache=active_cache,
    )
    logger.info(
        "派生任务装配：%s",
        ", ".join(spec.job_id for spec in derived) if derived else "无（无 latest 物化派生）",
    )
    sync_report = sync_algorithms(algorithm_store)
    logger.info(
        "算法登记同步：total=%s active=%s deprecated=%s events=%s",
        sync_report.total,
        sync_report.active,
        sync_report.deprecated,
        sync_report.events,
    )

    # 全市场基础信息与生命周期（TASK-3.35）：全局任务；能力门控（REFERENCE）
    registry_spec: TaskSpec | None = None
    if hub is not None and registry_source:
        if supports_capability(hub, registry_source, "reference"):
            registry_spec = register_market_registry_task(
                registry,
                engine,
                hub,
                source=registry_source,
                schedule=registry_schedule,
                cache=active_cache,
            )
            logger.info(
                "全市场登记任务装配：%s（source=%s schedule=%s）",
                registry_spec.job_id,
                registry_source,
                registry_schedule or "启动即跑一次",
            )
        else:
            logger.warning(
                "数据源 %s 未声明 reference 能力，跳过全市场登记任务注册", registry_source
            )
    elif not registry_source:
        logger.info("未配置 FDP_REGISTRY_SOURCE/FDP_SYNC_SOURCE：跳过全市场登记任务")

    # 质量扫描（TASK-3.5）：全局任务；调度可选（不配置 = 仅手动 / 管理界面触发）
    quality_datasets = _split_env("FDP_QUALITY_DATASETS") or DEFAULT_DATASETS
    quality_codes = _split_env("FDP_QUALITY_CODES") or (
        settings.codes if settings is not None else ()
    )
    quality_reconcile = _split_env("FDP_QUALITY_RECONCILE_CODES") or tuple(quality_codes[:2])
    quality_spec = register_quality_task(
        registry,
        engine,
        hub=hub,
        datasets=quality_datasets,
        lookback_days=_int_env("FDP_QUALITY_LOOKBACK_DAYS", 10),
        codes=quality_codes,
        reconcile_codes=quality_reconcile,
        schedule=(os.environ.get("FDP_QUALITY_SCHEDULE") or "").strip() or None,
    )
    logger.info(
        "质量任务装配：%s（datasets=%d codes=%d reconcile=%d schedule=%s）",
        quality_spec.job_id,
        len(quality_datasets),
        len(quality_codes),
        len(quality_reconcile),
        quality_spec.schedule or "手动触发",
    )

    # 批量导出（TASK-3.10）：全局任务；产物写入 FDP_EXPORT_DIR（compose 挂载卷）
    export_dir = (os.environ.get("FDP_EXPORT_DIR") or "").strip() or "data/exports"
    export_spec = register_export_task(
        registry,
        engine,
        export_dir=export_dir,
        entity_batch=_int_env("FDP_EXPORT_ENTITY_BATCH", DEFAULT_ENTITY_BATCH),
        chunk_days=_int_env("FDP_EXPORT_CHUNK_DAYS", DEFAULT_CHUNK_DAYS),
    )
    logger.info("导出任务装配：%s（dir=%s）", export_spec.job_id, export_dir)
    stale_exports = reconcile_stale_exports(engine)
    if stale_exports:
        logger.warning(
            "导出请求对账：%d 条 running 请求已置 failed（进程重启中断）", stale_exports
        )

    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s，准备停止", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    # 无调度的全局任务：入口显式提交「今天窗口」首灌（调度循环只按水位窗口，
    # window_provider 型任务不会自动触发；同窗口重复提交由 job_key 幂等）
    if registry_spec is not None and not registry_spec.schedule:
        today = utcnow().date()
        submitted = app.submit(
            registry.intent(registry_spec, window_start=today, window_end=today)
        )
        logger.info("全市场登记首灌已提交（%s）：%s", submitted, today.isoformat())

    app.start()
    logger.info("FinDataRuntime 已启动（role=%s）", config.role)
    try:
        while not stop.wait(1.0):
            pass
    finally:
        app.stop()
        logger.info("FinDataRuntime 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
