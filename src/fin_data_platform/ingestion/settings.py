"""Runtime 同步任务的启动配置（环境变量注入；TASK-3.6 切片 3）。

```
FDP_SYNC_CODES     代码清单（逗号分隔，canonical，如 600519.SH,000001.SZ）
FDP_SYNC_START     首次窗口起点（ISO 日期；水位缺失时使用）
FDP_SYNC_SOURCE    数据源（必填：tushare / akshare / ifind / wind / fuyao / baostock）
FDP_SYNC_SCHEDULE  调度表达式（可选：5 段 cron（UTC）或 interval:<秒>；
                   缺省为「轮询追平」——无 cron 定时，但启动即追平并周期轮询）
TUSHARE_TOKEN      Tushare 凭证（亦可 FIN_DATA_HUB_TUSHARE_TOKEN）
```

Hub 要求调用显式指定 source（不做隐式路由），故 ``FDP_SYNC_SOURCE`` 必填；
凭证与配置均由调用方 / 环境注入，不落仓库（doc-10 §6.4）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from fin_data_hub.enums import Source
from fin_data_platform.registry._util import to_date

ENV_CODES = "FDP_SYNC_CODES"
ENV_START = "FDP_SYNC_START"
ENV_SOURCE = "FDP_SYNC_SOURCE"
ENV_SCHEDULE = "FDP_SYNC_SCHEDULE"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SOURCE_VALUES = tuple(item.value for item in Source)


def _validate_schedule(value: str) -> None:
    """预校验调度表达式（真实验证交由 APScheduler；此处保证干净退出）。"""
    text = value.strip()
    if text.startswith("interval:"):
        raw = text.split(":", 1)[1]
        try:
            seconds = float(raw)
        except ValueError as exc:
            raise ValueError(f"{ENV_SCHEDULE} interval 数值非法: {raw!r}") from exc
        if seconds <= 0:
            raise ValueError(f"{ENV_SCHEDULE} interval 必须为正数: {raw!r}")
        return
    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(text, timezone="UTC")
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{ENV_SCHEDULE} 非法（应为 5 段 cron（UTC）或 interval:<秒>）: {value!r}"
        ) from exc


@dataclass(frozen=True, slots=True)
class SyncSettings:
    """同步任务装配配置（构造时校验）。"""

    codes: tuple[str, ...]
    start: date
    source: str
    schedule: str | None = None

    def __post_init__(self) -> None:
        if not self.codes:
            raise ValueError(f"{ENV_CODES} 未包含有效代码")
        if not self.source:
            raise ValueError(f"{ENV_SOURCE} 必须显式指定（可选: {', '.join(_SOURCE_VALUES)}）")
        if self.source not in _SOURCE_VALUES:
            raise ValueError(
                f"{ENV_SOURCE} 非法数据源: {self.source!r}（可选: {', '.join(_SOURCE_VALUES)}）"
            )
        if self.schedule is not None:
            _validate_schedule(self.schedule)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SyncSettings | None:
        """解析环境变量；未配置代码清单时返回 ``None``（不装配同步任务）。"""
        source_env = env if env is not None else os.environ
        raw_codes = (source_env.get(ENV_CODES) or "").strip()
        if not raw_codes:
            return None
        codes = tuple(dict.fromkeys(part.strip() for part in raw_codes.split(",") if part.strip()))
        raw_start = (source_env.get(ENV_START) or "").strip()
        if not raw_start:
            raise ValueError(f"配置了 {ENV_CODES} 时必须提供 {ENV_START}")
        start = to_date(raw_start) if _ISO_DATE.match(raw_start) else None
        if start is None:
            raise ValueError(f"{ENV_START} 非法日期: {raw_start!r}（应为 ISO 日期 YYYY-MM-DD）")
        source = (source_env.get(ENV_SOURCE) or "").strip()
        schedule = (source_env.get(ENV_SCHEDULE) or "").strip() or None
        return cls(codes=codes, start=start, source=source, schedule=schedule)
