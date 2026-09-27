"""幂等键与版本维度（doc-20 §4.2）。

```
job_key = hash(kind, job_id, scope, window, version_dimension?)
```

``version_dimension`` **只在语义会变的两类任务上启用**：

- ``derive`` → ``algorithm_id``（算法语义版本，非字典变更）；
- ``build_rm`` → 读模型 ``semantic_version``（``mart.<name>_v<N>``）；
- 其余 kind 无版本维度（append-only + 物理键已保证幂等）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from fin_data_platform.runtime.models import JobKind

#: kind → 版本维度语义（仅这两个 kind 允许版本维度）
VERSIONED_KINDS: dict[str, str] = {
    JobKind.DERIVE.value: "algorithm_id",
    JobKind.BUILD_RM.value: "semantic_version",
}


def validate_version_dimension(kind: str, version_dimension: str | None) -> None:
    """校验版本维度使用规则（doc-20 §4.2）。"""
    if kind in VERSIONED_KINDS:
        if not version_dimension:
            label = VERSIONED_KINDS[kind]
            raise ValueError(f"{kind} 任务必须有版本维度（{label}）")
    elif version_dimension:
        raise ValueError(
            f"{kind} 任务不应有版本维度（仅 derive / build_rm 版本感知）"
        )


def job_key(
    *,
    kind: str,
    job_id: str,
    scope: str = "",
    window_start: date | None = None,
    window_end: date | None = None,
    version_dimension: str | None = None,
) -> str:
    """生成稳定幂等键（sha256 截断十六进制）。"""
    validate_version_dimension(kind, version_dimension)
    payload = json.dumps(
        {
            "kind": kind,
            "job_id": job_id,
            "scope": scope,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
            "version_dimension": version_dimension,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def job_scope(job_id: str, dataset: str) -> str:
    """由任务标识约定推导 scope。

    ``sync.{dataset}.{code}`` → ``code``（按代码任务）；其它（全局任务，如
    ``sync.reference.market_registry``）→ ``""``。任务定义表不落 scope，
    触发意图须按同一约定推导，保证 job_key 与调度提交一致（幂等去重）。
    """
    prefix = f"sync.{dataset}."
    return job_id[len(prefix) :] if job_id.startswith(prefix) else ""
