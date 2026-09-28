"""领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

SORT_FIELDS = ("created_at", "score")


@dataclass(slots=True)
class Record:
    id: int
    name: str
    score: int
    created_at: int          # 单调递增的创建序号
    version: int = 1         # 乐观锁版本，每次成功更新 +1


@dataclass(slots=True)
class StagedChange:
    """事务中暂存的单个写操作（提交前不落库）。"""

    kind: str                # create / update / delete
    record_id: int | None
    name: str | None = None
    score: int | None = None
    expected_version: int | None = None
