"""API 依赖上下文与装配。

- **数据读取**（数据集字典 / 实体注册表）：走只读连接（``read_dsn``；
  未配置则回退写端）——与只读角色（``fdp_ro``）配套；
- **控制面**（任务/水位读取、同步意图提交）：属平台内部写入端，
  使用写连接；同步触发**只写 ``meta`` 队列**，由 Runtime 执行（不绕过控制面）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import Engine

from fin_data_platform.derived.store import AlgorithmStore, SqlAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.registry.reader import RegistryReader
from fin_data_platform.runtime.repository import MetaRepository, SqlMetaRepository
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.engine import create_read_engine, create_write_engine


@dataclass(slots=True)
class ApiContext:
    config: StorageConfig
    writer_engine: Engine
    read_engine: Engine
    meta: MetaRepository
    algorithms: AlgorithmStore
    registry: RegistryReader
    specs: dict[str, DatasetSpec]


def build_context(env: Mapping[str, str] | None = None) -> ApiContext:
    source = env if env is not None else os.environ
    config = StorageConfig.from_env(
        host_override=source.get("FDP_DATABASE_HOST")
    )
    writer_engine = create_write_engine(config)
    read_engine = create_read_engine(config)
    return ApiContext(
        config=config,
        writer_engine=writer_engine,
        read_engine=read_engine,
        meta=SqlMetaRepository(writer_engine),
        algorithms=SqlAlgorithmStore(writer_engine),
        registry=RegistryReader(read_engine),
        specs=load_all(),
    )


def get_context(request: Request) -> ApiContext:
    """FastAPI 依赖：从 app.state 取上下文（测试可注入替身）。"""
    return request.app.state.context  # type: ignore[no-any-return]
