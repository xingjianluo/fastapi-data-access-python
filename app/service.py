"""服务层：事务编排与入参业务校验。

事务（POST /records/transaction）：
  收到的多个操作先做结构性预校验，再整体作为一个暂存批次提交；
  存储层在工作副本上顺序套用，任一步版本/唯一/存在性校验失败即整批放弃。
"""

from __future__ import annotations

from app.errors import (
    BusinessValidationError,
    TransactionAbortedError,
)
from app.models import Record, StagedChange
from app.pagination import PageQuery
from app.repository import RecordRepository


def _record_dict(r: Record) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "score": r.score,
        "created_at": r.created_at,
        "version": r.version,
    }


class RecordService:
    def __init__(self, repository: RecordRepository) -> None:
        self.repo = repository

    # ---- 查询 ----
    def list_page(self, query: PageQuery) -> dict:
        page = self.repo.list_page(query)
        return {
            "items": [_record_dict(r) for r in page.items],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    def get_record(self, record_id: int) -> dict:
        return _record_dict(self.repo.get(record_id))

    # ---- 单条写 ----
    def create_record(self, name: str, score: int) -> dict:
        self._validate_name(name)
        self._validate_score(score)
        return _record_dict(self.repo.create(name.strip(), score))

    def update_record(
        self, record_id: int, payload: dict
    ) -> dict:
        expected_version = self._validate_version(payload)
        name = payload.get("name")
        score = payload.get("score")
        if name is not None:
            self._validate_name(name)
            name = name.strip()
        if score is not None:
            self._validate_score(score)
        if name is None and score is None:
            raise BusinessValidationError(
                "更新至少需要提供 name 或 score 中的一个字段",
                reason="empty_update",
            )
        return _record_dict(
            self.repo.update(record_id, name, score, expected_version)
        )

    def delete_record(self, record_id: int, payload: dict) -> None:
        expected_version = self._validate_version(payload)
        self.repo.delete(record_id, expected_version)

    # ---- 事务 ----
    def run_transaction(self, ops: list) -> list[dict]:
        if not isinstance(ops, list) or not ops:
            raise BusinessValidationError(
                "ops 必须是非空数组", reason="empty_transaction"
            )
        if len(ops) > 100:
            raise BusinessValidationError(
                "单事务操作数不能超过 100", reason="transaction_too_large"
            )

        changes: list[StagedChange] = []
        for idx, op in enumerate(ops):
            changes.append(self._build_change(op, idx))

        try:
            results = self.repo.commit_many(changes)
        except TransactionAbortedError:
            raise
        except Exception as exc:
            # 存储层抛出的任何冲突/容量错误都包装为“事务已中止并整体回滚”，
            # 同时保留原始错误分类（cause_reason / cause_type）。
            raise TransactionAbortedError.from_cause(exc) from exc
        return [
            {"index": i, "record": (_record_dict(r) if r else None)}
            for i, r in enumerate(results)
        ]

    def _build_change(self, op: object, idx: int) -> StagedChange:
        if not isinstance(op, dict):
            raise BusinessValidationError(
                f"第 {idx} 步必须是对象", reason="operation_not_object"
            )
        kind = op.get("op")
        if kind == "create":
            name = op.get("name")
            score = op.get("score")
            self._validate_name(name, idx)
            self._validate_score(score, idx)
            return StagedChange("create", None, name.strip(), score)  # type: ignore[union-attr]

        if kind in ("update", "delete"):
            record_id = op.get("id")
            if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id < 1:
                raise BusinessValidationError(
                    f"第 {idx} 步失败：id 必须是正整数", reason="invalid_id"
                )
            version = self._validate_version(op, idx)
            if kind == "delete":
                return StagedChange("delete", record_id, None, None, version)
            name = op.get("name")
            score = op.get("score")
            if name is not None:
                self._validate_name(name, idx)
                name = name.strip()
            if score is not None:
                self._validate_score(score, idx)
            if name is None and score is None:
                raise BusinessValidationError(
                    f"第 {idx} 步失败：更新至少需要 name 或 score",
                    reason="empty_update",
                )
            return StagedChange("update", record_id, name, score, version)

        raise BusinessValidationError(
            f"第 {idx} 步失败：未知操作类型 {kind!r}，"
            "允许 create/update/delete",
            reason="unknown_operation",
        )

    # ---- 字段校验 ----
    @staticmethod
    def _validate_name(name: object, idx: int | None = None) -> None:
        prefix = f"第 {idx} 步失败：" if idx is not None else ""
        if not isinstance(name, str) or not name.strip():
            raise BusinessValidationError(
                f"{prefix}name 必须是非空字符串", reason="invalid_name"
            )
        if len(name.strip()) > 100:
            raise BusinessValidationError(
                f"{prefix}name 长度不能超过 100", reason="name_too_long"
            )

    @staticmethod
    def _validate_score(score: object, idx: int | None = None) -> None:
        prefix = f"第 {idx} 步失败：" if idx is not None else ""
        if isinstance(score, bool) or not isinstance(score, int):
            raise BusinessValidationError(
                f"{prefix}score 必须是整数", reason="invalid_score"
            )
        if not 0 <= score <= 1_000_000:
            raise BusinessValidationError(
                f"{prefix}score 必须在 [0, 1000000] 区间内",
                reason="score_out_of_range",
            )

    @staticmethod
    def _validate_version(payload: dict, idx: int | None = None) -> int:
        prefix = f"第 {idx} 步失败：" if idx is not None else ""
        version = payload.get("expected_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise BusinessValidationError(
                f"{prefix}expected_version 必须是 >=1 的整数",
                reason="invalid_version",
            )
        return version
