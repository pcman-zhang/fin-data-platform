"""价格类派生算法（参考实现；复权口径与对账见 doc-8 §2）。

输入契约（doc-11 §4）：``inputs`` 键为 ``dataset.field``，值为 as-of 过滤后的
Arrow 表（至少包含该数据集 ``business_key`` 与所请求字段）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from fin_data_platform.derived.inputs import input_view_name
from fin_data_platform.derived.registry import register

if TYPE_CHECKING:
    import pyarrow as pa


#: 计算与读模型内联共用的 SQL 模板（视图名 = input_view_name(输入引用)）
QFQ_CLOSE_SQL = f"""
SELECT d.entity_id,
       d.trade_date,
       d.close * f.adj_factor / a.adj_factor AS qfq_close
FROM {input_view_name("cn_equity.daily_bar.close")} AS d
JOIN {input_view_name("cn_equity.adj_factor.adj_factor")} AS f
  ON f.entity_id = d.entity_id AND f.trade_date = d.trade_date
JOIN (
    SELECT entity_id, adj_factor
    FROM (
        SELECT entity_id,
               adj_factor,
               ROW_NUMBER() OVER (
                   PARTITION BY entity_id ORDER BY trade_date DESC
               ) AS _rank
        FROM {input_view_name("cn_equity.adj_factor.adj_factor")}
    ) ranked
    WHERE _rank = 1
) AS a
  ON a.entity_id = d.entity_id
ORDER BY d.entity_id, d.trade_date
"""


@register(algorithm_id="qfq_close_v1", owner="derived-engine", inline_sql=QFQ_CLOSE_SQL)
def qfq_close(inputs: Mapping[str, pa.Table], *, as_of: datetime) -> pa.Table:
    """前复权收盘价（qfq_close）。

    Formula:
        ``qfq_close = close × f / f_anchor``；``f_anchor`` 为该实体在 as_of 可见因子
        序列中按 ``trade_date`` 最大的 ``adj_factor``（doc-8 §2：``qfq ≈ raw × f / f_last``，
        因子为累计后复权口径，跨源不可直接混用）。

    PIT:
        输入已由引擎按 ``knowledge_time <= as_of`` 过滤（前视防护在读取层完成）；
        ``as_of`` 用于锚定基准日与审计标注，本函数不重新读数据。
    """
    import duckdb

    connection = duckdb.connect()
    try:
        for ref, table in inputs.items():
            connection.register(input_view_name(ref), table)
        return connection.execute(QFQ_CLOSE_SQL).to_arrow_table()
    finally:
        connection.close()
