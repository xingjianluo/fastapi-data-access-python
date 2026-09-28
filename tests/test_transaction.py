"""事务回滚与错误分类测试。

场景：
- 批量写中第 N 步业务校验失败 -> 整体回滚，前面成功的步骤也不落库；
- 批量写中版本冲突 -> 整体回滚，错误类型为 BATCH_ABORTED 且携带 cause；
- 业务校验错误（422）与唯一冲突（409）与基础设施错误（5xx）可区分；
- 资源被占满（模拟 SQLITE_FULL）翻译为 INFRA_STORAGE_FULL(507)。
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from app.db import translate_sqlite_error
from app.errors import (
    BatchAbortedError,
    InfraStorageFullError,
    InfraUnavailableError,
    ResourceAlreadyExistsError,
    ValidationFailedError,
)
from app.models import ArticleCreate, ArticleUpdate

logger = logging.getLogger("tests.transaction")


def _count(repo) -> int:
    return len(repo.list_page(100).items)


def test_batch_rolls_back_on_business_validation_failure(repo) -> None:
    ops = [
        ArticleCreate(id="b1", title="first", content=""),
        ArticleCreate(id="b2", title="", content=""),  # 第 2 步业务校验失败
        ArticleCreate(id="b3", title="third", content=""),
    ]
    logger.info("rollback input batch=%s decision=step2 blank-title -> rollback all", [o.id for o in ops])

    with pytest.raises(BatchAbortedError) as exc:
        repo.batch(ops)

    assert exc.value.http_status == 409
    assert exc.value.details["failed_index"] == 1
    assert exc.value.details["cause_code"] == "VALIDATION_FAILED"
    logger.info("rollback decision error=%s details=%s", exc.value.code.value, exc.value.details)

    assert _count(repo) == 0, "回滚后第一步的写入也必须不存在"


def test_batch_rolls_back_on_version_conflict(repo) -> None:
    repo.create(ArticleCreate(id="d1", title="orig", content="c"))
    ops = [
        ArticleCreate(id="d2", title="new", content="c"),
        ArticleUpdate(id="d1", expected_version=99, title="stale"),  # 版本不匹配
    ]
    logger.info("batch-conflict input ids=%s decision=version mismatch -> BATCH_ABORTED + rollback", ["d2", "d1"])

    with pytest.raises(BatchAbortedError) as exc:
        repo.batch(ops)
    assert exc.value.details["failed_index"] == 1
    assert exc.value.details["cause_code"] == "VERSION_CONFLICT"

    rows = {a.id: a for a in repo.list_page(100).items}
    assert "d2" not in rows, "同事务内新建的 d2 必须随回滚消失"
    assert rows["d1"].version == 1 and rows["d1"].title == "orig"


def test_empty_batch_is_business_error(repo) -> None:
    with pytest.raises(ValidationFailedError) as exc:
        repo.batch([])
    assert exc.value.reason == "batch_requires_at_least_one_op"


def test_single_create_violations_are_business_category(repo) -> None:
    repo.create(ArticleCreate(id="u1", title="t", content="c"))
    with pytest.raises(ResourceAlreadyExistsError) as exc:
        repo.create(ArticleCreate(id="u1", title="t2", content="c"))
    assert exc.value.http_status == 409
    assert exc.value.details == {"field": "id", "value": "u1"}
    logger.info("unique decision code=%s category=BUSINESS details=%s", exc.value.code.value, exc.value.details)


def test_sqlite_error_translation_table() -> None:
    """错误分类规则：底层 sqlite3 异常永远不直接外抛。"""
    cases = [
        (sqlite3.OperationalError("database is locked"), InfraUnavailableError, 503),
        (sqlite3.OperationalError("database disk image is malformed"), type(None), 500),  # 走内部错误分支
        (sqlite3.OperationalError("no such table: x"), type(None), 500),
    ]
    for raw, expected_cls, expected_status in cases:
        mapped = translate_sqlite_error(raw)
        logger.info(
            "translate input=%r -> code=%s http=%s",
            raw,
            mapped.code.value,
            mapped.http_status,
        )
        if expected_cls is not type(None):
            assert isinstance(mapped, expected_cls)
        assert mapped.http_status == expected_status


def test_storage_full_is_translated_to_infra_storage_full(repo, monkeypatch) -> None:
    """资源被占满：模拟 execute 抛 SQLITE_FULL，验证翻译为 507 而非透传。"""
    full = sqlite3.OperationalError("database or disk is full")
    full.sqlite_errorcode = 13  # SQLITE_FULL

    class _ExplodingConn:
        def execute(self, *_args, **_kwargs):
            raise full

        def close(self) -> None:
            pass

    @staticmethod
    def _explode(*_a, **_k):
        return _ExplodingConn()

    monkeypatch.setattr(repo.db, "_new_conn", _explode)
    logger.info("full input simulated SQLITE_FULL decision=expect INFRA_STORAGE_FULL(507), no raw leak")
    with pytest.raises(InfraStorageFullError) as exc:
        repo.create(ArticleCreate(id="z1", title="t", content="c"))
    assert exc.value.http_status == 507
    assert exc.value.code.value == "INFRA_STORAGE_FULL"


def test_busy_is_infra_distinct_from_business(repo) -> None:
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = 5  # SQLITE_BUSY
    mapped = translate_sqlite_error(busy)
    assert isinstance(mapped, InfraUnavailableError)
    assert mapped.http_status == 503
    logger.info("busy decision category=INFRASTRUCTURE code=%s", mapped.code.value)
