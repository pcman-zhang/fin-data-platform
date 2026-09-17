"""派生引擎命令行入口。

```bash
python -m fin_data_platform.derived --check   # 字典 ↔ 算法注册表一致性（不连库）
python -m fin_data_platform.derived --sync    # 同步 meta.algorithm_registry / algorithm_events
python -m fin_data_platform.derived --list    # 查看库中登记与升级台账
```
"""

from __future__ import annotations

import argparse
import logging

from fin_data_platform.derived.consistency import check_consistency
from fin_data_platform.derived.store import SqlAlgorithmStore
from fin_data_platform.derived.sync import describe_rows, sync_algorithms
from fin_data_platform.dictionary import load_all

logger = logging.getLogger("fin_data_platform.derived")


def _store() -> SqlAlgorithmStore:
    from fin_data_platform.storage.config import StorageConfig
    from fin_data_platform.storage.engine import create_write_engine

    return SqlAlgorithmStore(create_write_engine(StorageConfig.from_env()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fin-data-platform-derived",
        description="派生引擎：算法登记 / 一致性 / 升级台账（doc-11 §4）",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="一致性校验（不连库）")
    group.add_argument("--sync", action="store_true", help="同步登记表与升级台账（幂等）")
    group.add_argument("--list", action="store_true", help="列出库中登记与升级台账")
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    specs = load_all()

    if args.check:
        errors = check_consistency(specs)
        for error in errors:
            print(f"ERROR {error}")
        if errors:
            return 1
        print(f"OK 一致性通过（数据集 {len(specs)} 个）")
        return 0

    store = _store()
    if args.sync:
        report = sync_algorithms(store, specs)
        print(
            "OK 同步完成："
            f"total={report.total} active={report.active} "
            f"deprecated={report.deprecated} events={report.events}"
        )
        return 0

    print(describe_rows(store.list_all()))
    events = store.list_events()
    print(f"升级台账 {len(events)} 条：")
    for event in events:
        print(f"  {event.effective_from} {event.algorithm_id} {event.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
