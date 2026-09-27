"""受限表达式解析（doc-11 §3.4）：质量规则 ``expression`` 文本 → SQLAlchemy 条件。

支持范围（首期，与 doc-11 决策一致）：

- 比较（``== != < <= > >=``，含 Python 链式 ``a <= b <= c`` → AND）；
- 逻辑（``and / or / not``）、算术（``+ - * / %``）、圆括号、数值 / 字符串 / 布尔常量；
- 字段名必须是本数据集已登记字段（拼写错误即报错，不静默通过）。

一律拒绝函数调用、属性 / 下标、条件表达式与容器字面量——窗口 / 聚合 / join
属派生引擎职责（doc-11 §10 决策记录）。
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, not_, or_
from sqlalchemy.sql import ColumnElement

_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_ARITHMETIC = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}


def compile_expression(
    text: str, columns: Mapping[str, Any]
) -> tuple[ColumnElement[Any], tuple[str, ...]]:
    """编译表达式 →（SQL 条件, 引用字段名元组）。语法或字段非法即 ``ValueError``。"""
    if not text.strip():
        raise ValueError("表达式为空")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"表达式语法错误: {text!r}") from exc
    body = tree.body
    if not (
        isinstance(body, (ast.Compare, ast.BoolOp))
        or (isinstance(body, ast.UnaryOp) and isinstance(body.op, ast.Not))
    ):
        raise ValueError(f"表达式顶层必须是布尔条件: {text!r}")
    referenced: list[str] = []
    clause = _build(body, columns, referenced)
    return clause, tuple(referenced)


def _build(node: ast.AST, columns: Mapping[str, Any], referenced: list[str]) -> Any:
    if isinstance(node, ast.BoolOp):
        values = [_build(value, columns, referenced) for value in node.values]
        return and_(*values) if isinstance(node.op, ast.And) else or_(*values)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not_(_build(node.operand, columns, referenced))
        if isinstance(node.op, ast.USub):
            return -_build(node.operand, columns, referenced)
        raise ValueError(f"不支持的运算符: {ast.dump(node.op)}")
    if isinstance(node, ast.Compare):
        left = _build(node.left, columns, referenced)
        clauses: list[Any] = []
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _build(comparator, columns, referenced)
            build = _COMPARE.get(type(op))
            if build is None:
                raise ValueError(f"不支持的比较运算符: {ast.dump(op)}")
            clauses.append(build(left, right))
            left = right
        return clauses[0] if len(clauses) == 1 else and_(*clauses)
    if isinstance(node, ast.BinOp):
        build = _ARITHMETIC.get(type(node.op))
        if build is None:
            raise ValueError(f"不支持的算术运算符: {ast.dump(node.op)}")
        left = _build(node.left, columns, referenced)
        right = _build(node.right, columns, referenced)
        return build(left, right)
    if isinstance(node, ast.Name):
        column = columns.get(node.id)
        if column is None:
            raise ValueError(f"表达式引用了未登记字段: {node.id}")
        if node.id not in referenced:
            referenced.append(node.id)
        return column
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (bool, int, float, str)) or node.value is None:
            return node.value
        raise ValueError(f"不支持的常量: {node.value!r}")
    raise ValueError(f"不支持的表达式语法: {ast.dump(node)}")
