"""分页测试：游标稳定性、翻页期间写入/删除、排序值重复、非法参数。

数据完全使用本地内存表，无需外部依赖。
"""

from __future__ import annotations

import pytest

from app.errors import PaginationError
from app.models import StagedChange
from app.pagination import validate_page_params
from app.repository import RecordRepository
from app.storage import InMemoryTable


def _seed(repo: RecordRepository, n: int, *, tie_score: bool = False) -> None:
    for i in range(1, n + 1):
        repo.create(f"r{i}", (10 if tie_score else i))


def _walk(repo, sort_by="created_at", order="asc", limit=3,
          interleave=None):
    """翻完全部页，返回 (id 顺序, 页大小序列)。"""
    seen: list[int] = []
    sizes: list[int] = []
    cursor = None
    while True:
        query = validate_page_params(sort_by, order, limit, cursor)
        page = repo.list_page(query)
        ids = [r.id for r in page.items]
        seen.extend(ids)
        sizes.append(len(ids))
        if interleave is not None:
            interleave()
        if not page.next_cursor:
            break
        cursor = page.next_cursor
    return seen, sizes


def test_paging_stable_with_duplicate_sort_values(log_case):
    """排序值全部重复：键集游标靠 id 决胜，不重不漏。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    _seed(repo, 7, tie_score=True)
    seen, sizes = _walk(repo, sort_by="score", order="asc", limit=2)

    verdict = (
        "无重复=%s，无遗漏=%s" % (len(seen) == len(set(seen)), sorted(seen) == list(range(1, 8)))
    )
    log_case(
        "paging_duplicate_sort_values",
        {"rows": 7, "sort_by": "score", "order": "asc", "limit": 2},
        f"{verdict}，页大小序列={sizes}",
        extra={"seen_ids": seen},
    )
    assert len(seen) == len(set(seen))
    assert sorted(seen) == list(range(1, 8))
    assert sizes == [2, 2, 2, 1]


def test_paging_desc_with_duplicate_sort_values(log_case):
    table = InMemoryTable()
    repo = RecordRepository(table)
    _seed(repo, 6, tie_score=True)
    seen, _ = _walk(repo, sort_by="score", order="desc", limit=2)

    log_case(
        "paging_desc_duplicate_sort_values",
        {"rows": 6, "order": "desc", "limit": 2},
        "无重复=%s，无遗漏=%s" % (
            len(seen) == len(set(seen)), sorted(seen) == list(range(1, 7))),
        extra={"seen_ids": seen},
    )
    assert len(seen) == len(set(seen))
    assert sorted(seen) == list(range(1, 7))


def test_paging_with_inserts_during_walk(log_case):
    """翻页过程中持续写入新数据：游标界定扫描窗口，不重复不遗漏原始集合。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    _seed(repo, 5)  # id 1..5, created_at 1..5

    inserted = {"count": 0}

    def interleave():
        # 每页之间插入一条 created_at 更大的新数据（asc 时落在游标之后，
        # 会在后续页出现；关键是旧的 5 条必须且仅出现一次）
        n = inserted["count"] + 6
        repo.create(f"new{n}", 100)
        inserted["count"] += 1

    seen, _ = _walk(repo, sort_by="created_at", order="asc", limit=2,
                    interleave=interleave)

    original_seen = [i for i in seen if i <= 5]
    verdict = (
        "原始5条无重复=%s，原始5条无遗漏=%s"
        % (len(original_seen) == len(set(original_seen)),
           sorted(original_seen) == list(range(1, 6)))
    )
    log_case(
        "paging_inserts_during_walk",
        {"initial_rows": 5, "limit": 2, "inserts_between_pages": inserted["count"]},
        verdict,
        extra={"seen_ids": seen, "new_ids": [i for i in seen if i > 5]},
    )
    assert sorted(original_seen) == list(range(1, 6))
    assert len(original_seen) == 5
    # 整体同样不允许重复 id
    assert len(seen) == len(set(seen))


def test_paging_with_deletes_during_walk(log_case):
    """翻页过程中删除尚未访问的记录：不重复；已删记录的缺席可解释（游标窗口缩小）。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    _seed(repo, 6)

    state = {"page": 0}

    def interleave():
        state["page"] += 1
        if state["page"] == 1:
            # 第二页之前删除 id=3（created_at=3），它在游标之后
            table.commit([StagedChange(
                "delete", 3, None, None, 1)])

    seen, _ = _walk(repo, sort_by="created_at", order="asc", limit=2,
                    interleave=interleave)

    log_case(
        "paging_deletes_during_walk",
        {"initial_rows": 6, "deleted_mid_walk": 3, "limit": 2},
        "无重复=%s，被删记录不出现=%s，其余记录无遗漏=%s" % (
            len(seen) == len(set(seen)),
            3 not in seen,
            sorted(seen) == [1, 2, 4, 5, 6],
        ),
        extra={"seen_ids": seen},
    )
    assert 3 not in seen
    assert sorted(seen) == [1, 2, 4, 5, 6]
    assert len(seen) == len(set(seen))


@pytest.mark.parametrize("params,expected_reason", [
    ({"sort_by": "nope", "order": "asc", "limit": 10, "cursor": None},
     "invalid_sort_by"),
    ({"sort_by": "created_at", "order": "sideways", "limit": 10, "cursor": None},
     "invalid_order"),
    ({"sort_by": "created_at", "order": "asc", "limit": 0, "cursor": None},
     "limit_too_small"),
    ({"sort_by": "created_at", "order": "asc", "limit": 101, "cursor": None},
     "limit_too_large"),
    ({"sort_by": "created_at", "order": "asc", "limit": -3, "cursor": None},
     "limit_too_small"),
    ({"sort_by": "created_at", "order": "asc", "limit": 10, "cursor": "not-base64!!!"},
     "cursor_malformed"),
])
def test_invalid_pagination_params(params, expected_reason, log_case):
    with pytest.raises(PaginationError) as exc_info:
        validate_page_params(**params)
    reason = exc_info.value.reason
    log_case(
        f"invalid_pagination_{expected_reason}",
        params,
        f"拒绝原因={reason}，期望={expected_reason}，匹配={reason == expected_reason}",
    )
    assert reason == expected_reason


def test_cursor_sort_mismatch_rejected(log_case):
    """游标按 score 生成，却用 created_at 翻页：游标失效，原因可区分。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    _seed(repo, 3)
    page = repo.list_page(validate_page_params("score", "asc", 1, None))
    cursor = page.next_cursor

    with pytest.raises(PaginationError) as exc_info:
        validate_page_params("created_at", "asc", 1, cursor)

    log_case(
        "cursor_sort_mismatch",
        {"cursor_generated_with": "score", "requested_sort_by": "created_at"},
        f"拒绝原因={exc_info.value.reason}",
    )
    assert exc_info.value.reason == "cursor_sort_mismatch"
