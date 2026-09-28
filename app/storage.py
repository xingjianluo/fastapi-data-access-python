"""线程安全的内存表：模拟数据库的并发控制与资源约束。

- 所有读写在同一把锁下完成，保证原子可见；
- 写操作以“工作副本 + 提交时统一校验”的方式执行：
  先在 dict 拷贝上顺序套用全部变更并逐项校验，
  任何一步失败直接丢弃工作副本（事务回滚语义）；
- max_records 模拟容量打满，提交时触发 StorageFullError，
  属于基础设施错误，与业务校验失败相区分。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import replace

from app.errors import (
    RecordNotFoundError,
    StorageFullError,
    UniqueConstraintError,
    VersionConflictError,
)
from app.models import Record, StagedChange


class InMemoryTable:
    def __init__(self, max_records: int = 1000) -> None:
        self._lock = threading.RLock()
        self._rows: dict[int, Record] = {}
        self._next_id = 1
        self._clock = 0
        self.max_records = max_records

    # ---- 读 ----
    def snapshot(self) -> list[Record]:
        """返回当前数据的拷贝快照。"""
        with self._lock:
            return [replace(r) for r in self._rows.values()]

    def get(self, record_id: int) -> Record | None:
        with self._lock:
            row = self._rows.get(record_id)
            return replace(row) if row else None

    # ---- 写 ----
    def commit(self, changes: Iterable[StagedChange]) -> list[Record | None]:
        """原子提交一批变更，返回与 changes 对齐的结果（删除返回 None）。

        校验全部通过前不修改任何真实状态。
        """
        changes = list(changes)
        with self._lock:
            working = dict(self._rows)          # 工作副本
            next_id = self._next_id
            clock = self._clock
            results: list[Record | None] = []
            creates_in_batch = 0

            for idx, ch in enumerate(changes):
                if ch.kind == "create":
                    clock += 1
                    record = Record(
                        id=next_id,
                        name=self._require_name(ch.name, idx),
                        score=self._require_score(ch.score, idx),
                        created_at=clock,
                    )
                    self._check_name_unique(working, record.name, None, idx)
                    working[record.id] = record
                    results.append(replace(record))
                    next_id += 1
                    creates_in_batch += 1

                elif ch.kind == "update":
                    if ch.record_id is None:
                        raise ValueError("update 缺少 record_id")
                    current = working.get(ch.record_id)
                    if current is None:
                        raise RecordNotFoundError(
                            f"记录 {ch.record_id} 不存在，第 {idx} 步更新无法执行",
                            reason="record_not_found",
                        )
                    if ch.expected_version != current.version:
                        raise VersionConflictError(
                            "记录已被其他写入修改，版本不匹配: "
                            f"期望版本 {ch.expected_version}，当前版本 "
                            f"{current.version}",
                            reason="version_conflict",
                        )
                    new_name = current.name if ch.name is None else ch.name
                    new_score = current.score if ch.score is None else ch.score
                    self._check_name_unique(
                        working, new_name, ch.record_id, idx
                    )
                    updated = replace(
                        current,
                        name=new_name,
                        score=new_score,
                        version=current.version + 1,
                    )
                    working[updated.id] = updated
                    results.append(replace(updated))

                elif ch.kind == "delete":
                    if ch.record_id is None:
                        raise ValueError("delete 缺少 record_id")
                    current = working.get(ch.record_id)
                    if current is None:
                        raise RecordNotFoundError(
                            f"记录 {ch.record_id} 不存在，第 {idx} 步删除无法执行",
                            reason="record_not_found",
                        )
                    if ch.expected_version != current.version:
                        raise VersionConflictError(
                            "记录已被其他写入修改，版本不匹配: "
                            f"期望版本 {ch.expected_version}，当前版本 "
                            f"{current.version}",
                            reason="version_conflict",
                        )
                    del working[ch.record_id]
                    results.append(None)
                else:
                    raise ValueError(f"未知变更类型: {ch.kind!r}")

            # 容量属于基础设施约束：全部业务校验通过后才检查
            if len(working) > self.max_records:
                raise StorageFullError(
                    f"存储容量已满（上限 {self.max_records} 条），提交被拒绝",
                    reason="storage_full",
                )

            # 提交：工作副本转正
            self._rows = working
            self._next_id = next_id
            self._clock = clock
            return results

    # ---- 校验辅助 ----
    @staticmethod
    def _require_name(name: str | None, idx: int) -> str:
        from app.errors import BusinessValidationError

        if not isinstance(name, str) or not name.strip():
            raise BusinessValidationError(
                f"第 {idx} 步失败：name 必须是非空字符串",
                reason="invalid_name",
            )
        return name.strip()

    @staticmethod
    def _require_score(score: int | None, idx: int) -> int:
        from app.errors import BusinessValidationError

        if isinstance(score, bool) or not isinstance(score, int):
            raise BusinessValidationError(
                f"第 {idx} 步失败：score 必须是整数",
                reason="invalid_score",
            )
        return score

    @staticmethod
    def _check_name_unique(
        working: dict[int, Record],
        name: str,
        exclude_id: int | None,
        idx: int,
    ) -> None:
        from app.errors import UniqueConstraintError

        for rid, row in working.items():
            if rid != exclude_id and row.name == name:
                raise UniqueConstraintError(
                    f"第 {idx} 步失败：name={name!r} 已被记录 {rid} 占用",
                    reason="unique_constraint",
                )
