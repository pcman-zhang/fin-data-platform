"""FinDataRuntime 入口：``python -m fin_data_platform.runtime --role all|scheduler|worker``。

角色可拆进程（doc-20 §3.2）：``scheduler`` 只做调度与分发，``worker`` 只做执行，
两者仅凭 PostgreSQL 协调；``all`` 为单机默认。

同步任务由环境变量装配（TASK-3.6 切片 3）：
``FDP_SYNC_CODES`` / ``FDP_SYNC_START`` / ``FDP_SYNC_SOURCE`` / ``FDP_SYNC_SCHEDULE``；
未配置时启动为空 Runtime（仅控制面）。

派生任务（TASK-3.12）：字典 ``materialize=latest`` 的派生自动注册（``derive.*``）；
``refresh=scheduled`` 使用 ``FDP_DERIVE_SCHEDULE``（cron / interval:<秒>），未配置则仅手动触发。
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
from fin_data_platform.ingestion.bootstrap import build_sync_runtime
from fin_data_platform.ingestion.settings import SyncSettings
from fin_data_platform.runtime.app import RuntimeApp
from fin_data_platform.runtime.config import ROLES, RuntimeConfig
from fin_data_platform.runtime.registry import TaskRegistry
from fin_data_platform.storage.engine import create_write_engine

logger = logging.getLogger("fin_data_platform.runtime")


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
    if settings is not None:
        app = build_sync_runtime(
            config, settings, engine=engine, registry=registry, cache=active_cache
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

    stop = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s，准备停止", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

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
