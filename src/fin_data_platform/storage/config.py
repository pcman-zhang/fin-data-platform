"""存储配置：读写 DSN 分离（doc-10 §2；只读角色见后续切片）。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """写入/读取连接配置。

    - ``write_dsn``：内部写入端（按 schema 最小授权）；
    - ``read_dsn``：SDK/读模型只读连接；缺省回退 ``write_dsn``（本地开发）；
    - ``timescale``：是否启用 TimescaleDB 扩展语句（hypertable/压缩）。
    """

    write_dsn: str
    read_dsn: str | None = None
    timescale: bool = True
    #: 连接超时（秒）：网络不可达时快速失败（健康检查与启动就绪依赖）
    connect_timeout: float = 5.0
    #: 读连接语句超时（秒）：资源隔离（外部读取；0/None = 关闭；写端与导出不受限）
    statement_timeout: float | None = 30.0

    @property
    def reader_dsn(self) -> str:
        return self.read_dsn or self.write_dsn

    @classmethod
    def from_env(
        cls,
        *,
        database: str = "fin_data_platform",
        host_override: str | None = None,
        prefix: str = "DATABASE_",
    ) -> StorageConfig:
        """从环境变量构建 DSN（凭证不落盘）。

        写端：``DATABASE_HOST/PORT/USER/PASSWORD``（``DATABASE_NAME`` 可选）；
        读端：``DATABASE_READ_USER/PASSWORD``（可选；``DATABASE_READ_HOST/PORT/NAME``
        缺省沿用写端）——实现读写 DSN 分离（reader 仅有只读角色权限）；
        ``DATABASE_CONNECT_TIMEOUT`` 可选（秒，默认 5）；``host_override`` 用于
        DHCP 等地址变动的场景。
        """
        from sqlalchemy.engine import URL

        user = os.environ.get(f"{prefix}USER")
        password = os.environ.get(f"{prefix}PASSWORD")
        host = host_override or os.environ.get(f"{prefix}HOST")
        port = os.environ.get(f"{prefix}PORT", "5432")
        name = os.environ.get(f"{prefix}NAME", database)
        if not (user and password and host):
            raise ValueError(
                f"缺少 {prefix}USER/{prefix}PASSWORD/{prefix}HOST 环境变量"
            )
        raw_timeout = os.environ.get(f"{prefix}CONNECT_TIMEOUT", "5")
        try:
            connect_timeout = float(raw_timeout)
        except ValueError as exc:
            raise ValueError(
                f"{prefix}CONNECT_TIMEOUT 非法（应为正数秒）: {raw_timeout!r}"
            ) from exc
        if connect_timeout <= 0:
            raise ValueError(
                f"{prefix}CONNECT_TIMEOUT 必须为正数秒: {raw_timeout!r}"
            )
        url = URL.create(
            "postgresql+psycopg",
            username=user,
            password=password,
            host=host,
            port=int(port),
            database=name,
        )
        read_prefix = f"{prefix}READ_"
        read_user = os.environ.get(f"{read_prefix}USER")
        read_password = os.environ.get(f"{read_prefix}PASSWORD")
        # 空串等同未配置（compose 常注入空值）；只配一侧 → 显式报错
        if bool(read_user) != bool(read_password):
            raise ValueError(
                f"{read_prefix}USER 与 {read_prefix}PASSWORD 必须同时提供"
            )
        raw_statement_timeout = os.environ.get(f"{prefix}STATEMENT_TIMEOUT", "30").strip()
        statement_timeout: float | None
        if not raw_statement_timeout or raw_statement_timeout == "0":
            statement_timeout = None
        else:
            try:
                statement_timeout = float(raw_statement_timeout)
            except ValueError as exc:
                raise ValueError(
                    f"{prefix}STATEMENT_TIMEOUT 非法（应为非负秒数）: {raw_statement_timeout!r}"
                ) from exc
            if statement_timeout < 0:
                raise ValueError(
                    f"{prefix}STATEMENT_TIMEOUT 不能为负: {raw_statement_timeout!r}"
                )
        read_dsn: str | None = None
        if read_user and read_password:
            read_url = URL.create(
                "postgresql+psycopg",
                username=read_user,
                password=read_password,
                host=os.environ.get(f"{read_prefix}HOST") or host,
                port=int(os.environ.get(f"{read_prefix}PORT", port)),
                database=os.environ.get(f"{read_prefix}NAME", name),
            )
            read_dsn = read_url.render_as_string(hide_password=False)
        return cls(
            write_dsn=url.render_as_string(hide_password=False),
            read_dsn=read_dsn,
            connect_timeout=connect_timeout,
            statement_timeout=statement_timeout,
        )
