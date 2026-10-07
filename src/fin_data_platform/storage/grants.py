"""只读角色与授权（个人平台：只读 / 可写两分）。

**当前实现**：单一只读权限角色 ``fdp_ro``（NOLOGIN）——
``mart``（读模型出口）+ ``ref``（参照数据）+ 各数据域 canonical 的 ``USAGE/SELECT``；
``raw`` / ``meta`` **不授权**；``public.alembic_version`` 仅授 ``SELECT``（客户端连接
兼容校验用，仅暴露版本字符串）；写权限一概不授（由数据库强制）。

**演进方向（文档化，不在本期实现）**：按域拆分为 ``fdp_ro_<domain>``
（基础角色 + 单域 canonical），实现逐域最小授权；见配置手册授权矩阵。

登录用户由调用方/运维创建并继承权限角色（密码不经过代码与仓库）：

```
CREATE ROLE app_ro LOGIN PASSWORD '...';
GRANT fdp_ro TO app_ro;
```
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Iterable, Sequence

from sqlalchemy import Engine, text

from fin_data_platform.dictionary import load_all

#: 只读权限角色（NOLOGIN；仅授权载体）
READONLY_ROLE = "fdp_ro"

#: 内部 schema：不向只读角色开放
INTERNAL_SCHEMAS = frozenset({"raw", "meta", "public"})

#: 共享只读 schema（不随数据域变化）
SHARED_SCHEMAS: tuple[str, ...] = ("mart", "ref")


def dictionary_domains() -> list[str]:
    """字典中的数据域（canonical schema）。"""
    return sorted({spec.domain for spec in load_all().values()})


def readable_schemas(domains: Iterable[str] | None = None) -> list[str]:
    """只读角色可见 schema：``mart`` + ``ref`` + 数据域（排除内部 schema；去重）。"""
    values = dictionary_domains() if domains is None else list(domains)
    schemas = [*SHARED_SCHEMAS, *sorted(set(values))]
    return [
        schema
        for schema in dict.fromkeys(schemas)
        if schema not in INTERNAL_SCHEMAS
    ]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    """SQL 字符串字面量（单引号翻倍）。"""
    return "'" + value.replace("'", "''") + "'"


#: 允许的角色名（PostgreSQL 标识符子集；防注入与绑定参数歧义）
_ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_role(role: str) -> str:
    """校验角色名（CLI 传入；不合法直接报错）。"""
    if not _ROLE_PATTERN.match(role):
        raise ValueError(f"角色名非法（仅字母/数字/下划线）: {role!r}")
    return role


def readonly_statements(
    *,
    role: str = READONLY_ROLE,
    domains: Iterable[str] | None = None,
    writer: str | None = None,
) -> list[str]:
    """生成幂等授权语句（可审查、可测试）。

    - ``USAGE`` + ``SELECT``：mart / ref / 各数据域；
    - ``EXECUTE``：mart 表函数（如 ``entity_asof``）；
    - 可选 ``DEFAULT PRIVILEGES``：由 ``writer`` 未来创建的对象自动授权。
    """
    validate_role(role)
    quoted_role = _quote(role)
    schemas = readable_schemas(domains)
    statements = [
        "DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_literal(role)}) "
        f"THEN CREATE ROLE {quoted_role} NOLOGIN; END IF; "
        "END $$;"
    ]
    for schema in schemas:
        quoted = _quote(schema)
        statements.append(f"GRANT USAGE ON SCHEMA {quoted} TO {quoted_role};")
        statements.append(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {quoted} TO {quoted_role};"
        )
    statements.append(
        f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {_quote('mart')} TO {quoted_role};"
    )
    # 迁移修订号（SDK / 客户端连接兼容校验用；仅暴露版本字符串，显式最小授权）
    statements.append(
        f"GRANT SELECT ON TABLE public.alembic_version TO {quoted_role};"
    )
    if writer:
        quoted_writer = _quote(writer)
        for schema in schemas:
            quoted = _quote(schema)
            statements.append(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {quoted_writer} IN SCHEMA {quoted} "
                f"GRANT SELECT ON TABLES TO {quoted_role};"
            )
        statements.append(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {quoted_writer} IN SCHEMA {_quote('mart')} "
            f"GRANT EXECUTE ON FUNCTIONS TO {quoted_role};"
        )
    return statements


def current_user(engine: Engine) -> str:
    """数据库当前用户（写入端 owner；用于 DEFAULT PRIVILEGES）。"""
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT current_user")).scalar_one())


def apply_readonly_roles(
    engine: Engine,
    *,
    role: str = READONLY_ROLE,
    domains: Iterable[str] | None = None,
    writer: str | None = None,
) -> Sequence[str]:
    """执行授权（幂等；返回已执行语句便于审计）。"""
    statements = readonly_statements(
        role=role, domains=domains, writer=writer or current_user(engine)
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    return statements


def main(argv: Sequence[str] | None = None) -> int:
    """CLI：按环境执行只读授权（供部署一次性服务；幂等）。"""
    from fin_data_platform.storage.config import StorageConfig
    from fin_data_platform.storage.engine import create_write_engine

    parser = argparse.ArgumentParser(
        prog="fin-data-platform-grants",
        description="只读角色授权（个人平台：只读 / 可写两分；幂等）",
    )
    parser.add_argument(
        "--role",
        default=READONLY_ROLE,
        type=validate_role,
        help="只读权限角色名（字母/数字/下划线）",
    )
    parser.add_argument(
        "--domain", action="append", default=None, help="数据域（可多次；缺省取字典）"
    )
    args = parser.parse_args(argv)

    config = StorageConfig.from_env(
        host_override=os.environ.get("FDP_DATABASE_HOST")
    )
    engine = create_write_engine(config)
    statements = apply_readonly_roles(
        engine, role=args.role, domains=args.domain or None
    )
    print(f"只读授权完成：role={args.role} 语句={len(statements)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
