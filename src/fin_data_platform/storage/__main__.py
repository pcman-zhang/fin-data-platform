"""存储层运维 CLI：``python -m fin_data_platform.storage --migrate --seed``。"""

from __future__ import annotations

import argparse
import os

from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.engine import create_write_engine
from fin_data_platform.storage.migrations import upgrade
from fin_data_platform.storage.seed import seed_reference


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="存储层运维：Alembic 迁移（幂等）+ 参考数据种子（一次性首灌，已灌后为空操作）"
    )
    parser.add_argument("--migrate", action="store_true", help="执行 Alembic 迁移到最新修订")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="导入包内参考数据种子（ref.market/ref.trade_calendar；一次性首灌，幂等）",
    )
    args = parser.parse_args(argv)
    if not (args.migrate or args.seed):
        parser.print_help()
        return 2
    # 与迁移共用同一配置（含 FDP_DATABASE_HOST 覆盖），避免两个动作指向不同库
    config = StorageConfig.from_env(host_override=os.environ.get("FDP_DATABASE_HOST"))
    if args.migrate:
        upgrade(config.write_dsn)
        print("OK 迁移完成（head）")
    if args.seed:
        inserted = seed_reference(create_write_engine(config))
        print(f"OK 种子导入：{inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
