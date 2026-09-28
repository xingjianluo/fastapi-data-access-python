"""事务测试：多操作原子提交、失败整体回滚、错误分类。"""

from __future__ import annotations

import pytest

from app.errors import (
    BusinessValidationError,
    StorageFullError,
    TransactionAbortedError,
)
from app.models import StagedChange
from app.repository import RecordRepository
from app.service import RecordService
from app.storage import InMemoryTable


@pytest.fixture
def service():
    return RecordService(RecordRepository(InMemoryTable(max_records=3)))


def test_transaction_all_commit_or_all_rollback(service, log_case):
    """三步事务，第二步版本冲突：三步全部不落库。"""
    first = service.create_record("a", 1)
    before_rows = len(service.repo.table.snapshot())
    ops = [
        {"op": "create", "name": "b", "score": 2},
        {"op": "update", "id": first["id"], "score": 99,
         "expected_version": 999},  # 版本冲突
        {"op": "create", "name": "c", "score": 3},
    ]
    with pytest.raises(TransactionAbortedError) as exc_info:
        service.run_transaction(ops)

    after_rows = len(service.repo.table.snapshot())
    err = exc_info.value
    log_case(
        "tx_rollback_on_version_conflict",
        {"ops": ops, "rows_before": before_rows},
        "事务中止=%s，原始分类=%s/%s，行数不变=%s"
        % (err.error_type, err.cause_type, err.cause_reason,
           after_rows == before_rows),
        extra={"http_status": err.http_status, "rows_after": after_rows},
    )
    assert after_rows == before_rows
    assert err.cause_type == "version_conflict"
    # 名称 b/c 都不应存在（第一步也被回滚）
    names = {r.name for r in service.repo.table.snapshot()}
    assert "b" not in names and "c" not in names


def test_transaction_unique_conflict_classification(service, log_case):
    """唯一约束导致的回滚：cause_type=unique_conflict，HTTP 409。"""
    service.create_record("dup", 1)
    ops = [
        {"op": "create", "name": "x", "score": 1},
        {"op": "create", "name": "dup", "score": 2},
    ]
    with pytest.raises(TransactionAbortedError) as exc_info:
        service.run_transaction(ops)

    err = exc_info.value
    log_case(
        "tx_rollback_on_unique_conflict",
        {"ops": ops},
        "cause_type=%s，http_status=%d，x未残留=%s"
        % (err.cause_type, err.http_status,
           "x" not in {r.name for r in service.repo.table.snapshot()}),
    )
    assert err.cause_type == "unique_conflict"
    assert err.http_status == 409


def test_transaction_storage_full_is_infrastructure(service, log_case):
    """容量打满：归为基础设施错误（storage_full / 507），与业务校验相区分。"""
    service.create_record("a", 1)
    service.create_record("b", 1)  # 已占 2/3
    ops = [
        {"op": "create", "name": "c", "score": 1},
        {"op": "create", "name": "d", "score": 1},  # 超过上限 3
    ]
    with pytest.raises(TransactionAbortedError) as exc_info:
        service.run_transaction(ops)
    err = exc_info.value

    log_case(
        "tx_storage_full_classification",
        {"max_records": 3, "existing": 2, "creates_in_tx": 2},
        "cause_type=%s（基础设施），http_status=%d，容量检查前的c也回滚=%s"
        % (err.cause_type, err.http_status,
           len(service.repo.table.snapshot()) == 2),
    )
    assert err.cause_type == "storage_full"
    assert err.http_status == 507
    assert isinstance(err.__cause__, StorageFullError)
    assert len(service.repo.table.snapshot()) == 2


def test_business_validation_distinct_from_infrastructure(service, log_case):
    """字段非法：422 业务校验，不进入存储层；与 5xx 基础设施错误可区分。"""
    ops = [{"op": "create", "name": "ok", "score": 1},
           {"op": "create", "name": "", "score": 1}]
    with pytest.raises(BusinessValidationError) as exc_info:
        service.run_transaction(ops)

    err = exc_info.value
    log_case(
        "tx_business_validation_error",
        {"ops": ops},
        "error_type=%s，reason=%s，http_status=%d（业务类4xx，非基础设施5xx）"
        % (err.error_type, err.reason, err.http_status),
    )
    assert err.reason == "invalid_name"
    assert err.http_status == 422
    # 预校验失败，整个事务未提交
    assert service.repo.table.snapshot() == []


def test_transaction_mixed_ops_success(service, log_case):
    """create+update+delete 在同一事务内全部成功。"""
    r1 = service.create_record("a", 1)
    r2 = service.create_record("b", 2)
    ops = [
        {"op": "create", "name": "c", "score": 3},
        {"op": "update", "id": r1["id"], "score": 10,
         "expected_version": r1["version"]},
        {"op": "delete", "id": r2["id"],
         "expected_version": r2["version"]},
    ]
    results = service.run_transaction(ops)
    rows = service.repo.table.snapshot()
    log_case(
        "tx_mixed_success",
        {"ops": ops},
        "返回结果数=%d，最终行数=%d（c新增/a更新/b删除），a版本=2"
        % (len(results), len(rows)),
        extra={"results": results},
    )
    assert len(results) == 3
    by_name = {r.name: r for r in rows}
    assert set(by_name) == {"a", "c"}
    assert by_name["a"].score == 10 and by_name["a"].version == 2


def test_empty_transaction_rejected(service, log_case):
    with pytest.raises(BusinessValidationError) as exc_info:
        service.run_transaction([])
    log_case(
        "tx_empty_rejected",
        {"ops": []},
        "reason=%s" % exc_info.value.reason,
    )
    assert exc_info.value.reason == "empty_transaction"


def test_transaction_create_then_update_same_batch(service, log_case):
    """同一事务内先 create 后引用其返回 id 再 update：工作副本内顺序可见。"""
    # 先在独立事务中拿到 create 的 id 生成规律：本用例直接用固定 id=1
    # （空表创建，首条 id 必为 1）
    ops = [
        {"op": "create", "name": "fresh", "score": 1},
        {"op": "update", "id": 1, "score": 42, "expected_version": 1},
    ]
    results = service.run_transaction(ops)
    rows = service.repo.table.snapshot()
    log_case(
        "tx_create_then_update",
        {"ops": ops},
        "最终仅1行=%s，score=42 生效=%s，版本=2=%s"
        % (len(rows) == 1, rows[0].score == 42, rows[0].version == 2),
        extra={"results": results},
    )
    assert len(rows) == 1
    assert rows[0].score == 42
    assert rows[0].version == 2


def test_repository_level_partial_batch_rollback(log_case):
    """直接对存储层提交批次：最后一步失败时，工作副本整体丢弃。"""
    table = InMemoryTable()
    with pytest.raises(Exception):
        table.commit([
            StagedChange("create", None, "x", 1),
            StagedChange("create", None, "y", 2),
            StagedChange("create", None, None, 3),  # 非法 name
        ])
    log_case(
        "storage_batch_rollback",
        {"batch_size": 3, "failing_step": 2},
        "提交后表为空=%s" % (table.snapshot() == []),
    )
    assert table.snapshot() == []
