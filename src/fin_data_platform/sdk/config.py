"""SDK 连接配置（doc-21 §2 / doc-2 §6.4）：双模式，凭证只经注入，不读仓库文件。

环境变量（均在调用方环境注入，不落仓库）：

| 变量 | 说明 |
|---|---|
| ``FDP_SDK_MODE`` | ``direct``（默认）/ ``rest`` |
| ``FDP_SDK_DSN`` | 直连只读 DSN（缺省由 ``DATABASE_READ_*`` / ``DATABASE_*`` 组装） |
| ``FDP_SDK_CONTROL_DSN`` | 直连控制面 DSN（``meta`` 写权限；缺省由 ``DATABASE_*`` 组装） |
| ``FDP_SDK_REST_URL`` | REST 后端地址（``rest`` 模式；默认 ``http://127.0.0.1:8000``） |
| ``FDP_SDK_TIMEOUT`` | 请求超时（秒，默认 30） |
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, model_validator


class SdkMode(StrEnum):
    DIRECT = "direct"
    REST = "rest"


class SdkConfig(BaseModel):
    """SDK 连接配置（冻结：构造后不可变）。"""

    model_config = ConfigDict(frozen=True)

    mode: SdkMode = SdkMode.DIRECT
    #: 直连只读 DSN（data 面；建议只读角色）
    dsn: str | None = None
    #: 直连控制面 DSN（meta 写权限；仅 direct 模式提交意图需要）
    control_dsn: str | None = None
    #: REST 后端地址（rest 模式）
    rest_url: str = "http://127.0.0.1:8000"
    timeout: float = 30.0
    #: 首次使用时的连接 / schema 兼容校验
    check_compatibility: bool = True

    @model_validator(mode="after")
    def _validate(self) -> SdkConfig:
        if self.mode == SdkMode.DIRECT and not self.dsn:
            raise ValueError(
                "direct 模式需要 dsn（FDP_SDK_DSN 或 DATABASE_* 环境变量）"
            )
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SdkConfig:
        """从环境变量构建（缺省 ``direct``；``FDP_SDK_DSN`` 优先于 ``DATABASE_*``）。"""
        source = env if env is not None else os.environ
        mode = (source.get("FDP_SDK_MODE") or "direct").strip().lower()
        dsn = (source.get("FDP_SDK_DSN") or "").strip() or _dsn_from_parts(
            source, read_only=True
        )
        control_dsn = (source.get("FDP_SDK_CONTROL_DSN") or "").strip() or (
            _dsn_from_parts(source, read_only=False)
        )
        return cls(
            mode=SdkMode(mode),
            dsn=dsn or None,
            control_dsn=control_dsn or None,
            rest_url=(source.get("FDP_SDK_REST_URL") or "http://127.0.0.1:8000").strip(),
            timeout=float(source.get("FDP_SDK_TIMEOUT") or 30.0),
        )


def _dsn_from_parts(source: Mapping[str, str], *, read_only: bool) -> str | None:
    """由 ``DATABASE_*``（只读优先 ``DATABASE_READ_*``）组装 DSN；缺凭证返回 None。

    与 :class:`fin_data_platform.storage.config.StorageConfig` 同口径：只读模式优先
    ``DATABASE_READ_HOST/PORT/NAME``；凭证按 userinfo 规则完整转义（``/`` 亦转义）。
    """
    prefix = "DATABASE_READ_" if read_only else "DATABASE_"
    fallback = "DATABASE_" if read_only else None
    user = source.get(f"{prefix}USER") or (source.get(fallback + "USER") if fallback else None)
    password = source.get(f"{prefix}PASSWORD") or (
        source.get(fallback + "PASSWORD") if fallback else None
    )
    if not user or not password:
        return None
    host = source.get(f"{prefix}HOST") or source.get("DATABASE_HOST", "127.0.0.1")
    port = source.get(f"{prefix}PORT") or source.get("DATABASE_PORT", "5432")
    name = source.get(f"{prefix}NAME") or source.get("DATABASE_NAME", "fin_data_platform")
    return (
        f"postgresql+psycopg://{quote(str(user), safe='')}:"
        f"{quote(str(password), safe='')}@{host}:{port}/{name}"
    )
