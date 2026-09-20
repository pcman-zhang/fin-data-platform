"""参考数据种子 CLI：``python -m fin_data_platform.storage --seed``。"""

from __future__ import annotations

import argparse

from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.engine import create_write_engine
from fin_data_platform.storage.seed import seed_reference


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="参考数据种子导入（幂等）")
    parser.add_argument("--seed", action="store_true", help="导入 ref.market/ref.trade_calendar")
    args = parser.parse_args(argv)
    if not args.seed:
        parser.print_help()
        return 2
    inserted = seed_reference(create_write_engine(StorageConfig.from_env()))
    print(f"OK 种子导入：{inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
