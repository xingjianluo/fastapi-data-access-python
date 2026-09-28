"""键集分页测试。

覆盖：
- 正常翻页不重不漏；
- 翻页过程中写入新数据；
- 排序值（created_at）重复；
- 翻页过程中删除数据；
- 非法分页参数（不同拒绝原因）；
- 游标无法识别 / 游标锚点失效。
"""

from __future__ import annotations

import logging

import pytest

from app.errors import InvalidCursorError, InvalidPaginationParamError, StaleCursorError
from app.models import ArticleCreate
from app.pagination import decode_cursor, encode_cursor

logger = logging.getLogger("tests.pagination")


def _seed_direct(conn, rows: list[tuple[str, str, str]], *, manage_tx: bool = False) -> None:
    """rows: (id, created_at, title)；直接写库以精确控制排序值。

    manage_tx=True 时由本函数用 with conn 管理事务（独立连接场景）；
    在外部 transaction() 内调用时必须为 False，不得提前提交外层事务。
    """
    if manage_tx:
        with conn:
            for article_id, created_at, title in rows:
                conn.execute(
                    "INSERT INTO articles (id, title, content, version, created_at, updated_at) "
                    "VALUES (?, ?, '', 1, ?, ?)",
                    (article_id, title, created_at, created_at),
                )
        return
    for article_id, created_at, title in rows:
        conn.execute(
            "INSERT INTO articles (id, title, content, version, created_at, updated_at) "
            "VALUES (?, ?, '', 1, ?, ?)",
            (article_id, title, created_at, created_at),
        )


def _all_ids(repo, page_size: int = 2) -> tuple[list[str], list[str]]:
    """从首页一直翻到末页，返回 (收集到的id顺序, 每页next_cursor)。"""
    seen: list[str] = []
    cursors: list[str] = []
    cursor = None
    while True:
        page = repo.list_page(page_size, cursor)
        seen.extend(a.id for a in page.items)
        cursors.append(str(page.next_cursor))
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return seen, cursors


def test_basic_pagination_no_duplicate_or_missing(repo, database) -> None:
    with database.conn() as conn:
        _seed_direct(conn, [(f"a{i}", f"2026-01-01T00:00:0{i}:00+00:00", f"t{i}") for i in range(1, 6)], manage_tx=True)

    seen, _ = _all_ids(repo, page_size=2)
    logger.info("test_basic input 5 rows page_size=2 decision=compare order and uniqueness; seen=%s", seen)

    assert seen == [f"a{i}" for i in range(5, 0, -1)]
    assert len(seen) == len(set(seen)) == 5


def test_new_rows_inserted_during_paging_do_not_duplicate_or_drop(repo, database) -> None:
    with database.conn() as conn:
        _seed_direct(conn, [(f"a{i}", f"2026-01-01T00:00:0{i}:00+00:00", f"t{i}") for i in range(1, 5)], manage_tx=True)

    page1 = repo.list_page(2, None)
    first_page_ids = [a.id for a in page1.items]
    logger.info("new-row test page1=%s next=%s", first_page_ids, page1.next_cursor)

    # 翻页间隙写入：一条“更老”（排在最后），一条“更新”（排在最前）。
    with database.transaction() as conn:
        _seed_direct(conn, [("old-x", "2025-12-31T00:00:00+00:00", "old"), ("new-y", "2026-02-01T00:00:00+00:00", "new")], manage_tx=False)

    seen = list(first_page_ids)
    cursor = page1.next_cursor
    while cursor is not None:
        page = repo.list_page(2, cursor)
        seen.extend(a.id for a in page.items)
        cursor = page.next_cursor

    logger.info(
        "new-row test decision=keyset: original rows must appear exactly once; "
        "older-inserted row is reached; newer-than-anchor row belongs to an earlier page; seen=%s",
        seen,
    )
    # 原 4 行一行不少、一行不重。
    assert len(seen) == len(set(seen))
    assert {i for i in seen if i.startswith("a")} == {"a1", "a2", "a3", "a4"}
    # 翻页期间插入的“更老”行排在游标方向内，应能被翻到。
    assert "old-x" in seen
    # 比首页锚点更新的行属于更早的页，从旧游标继续翻页看不到它——
    # 从头重新发起一次查询才能看到，且它只出现一次。
    fresh_seen, _ = _all_ids(repo, page_size=2)
    assert len(fresh_seen) == len(set(fresh_seen)) == 6
    assert "new-y" in fresh_seen and fresh_seen[0] == "new-y"


def test_duplicate_sort_values_are_disambiguated_by_id(repo, database) -> None:
    ts = "2026-01-01T00:00:00+00:00"
    with database.conn() as conn:
        _seed_direct(conn, [("k1", ts, "x"), ("k2", ts, "x"), ("k3", ts, "x"), ("k4", ts, "x")], manage_tx=True)

    seen, _ = _all_ids(repo, page_size=2)
    logger.info("duplicate-ts test all same created_at; seen=%s decision=id tie-break must be unique", seen)
    assert len(seen) == len(set(seen)) == 4
    assert set(seen) == {"k1", "k2", "k3", "k4"}
    # 相同 created_at 下按 id DESC。
    assert seen == sorted(seen, reverse=True)


def test_deletion_between_pages_does_not_duplicate(repo, database) -> None:
    with database.conn() as conn:
        _seed_direct(conn, [(f"a{i}", f"2026-01-01T00:00:0{i}:00+00:00", f"t{i}") for i in range(1, 6)], manage_tx=True)

    page1 = repo.list_page(2, None)
    # 删除尚未翻到的 a3，再删除锚点之外更老的 a1。
    with database.transaction() as conn:
        conn.execute("DELETE FROM articles WHERE id IN ('a3', 'a1')")

    seen = [a.id for a in page1.items]
    cursor = page1.next_cursor
    while cursor is not None:
        page = repo.list_page(2, cursor)
        seen.extend(a.id for a in page.items)
        cursor = page.next_cursor

    logger.info("delete test seen=%s decision=no dup; survivors exactly once", seen)
    assert len(seen) == len(set(seen))
    assert set(seen) == {"a2", "a4", "a5"}


def test_invalid_limit_param_reasons(repo) -> None:
    for bad, reason in [(0, "limit_not_positive"), (-3, "limit_not_positive"), (101, "limit_too_large")]:
        logger.info("invalid-limit input limit=%s expected reason=%s", bad, reason)
        with pytest.raises(InvalidPaginationParamError) as exc:
            repo.list_page(bad)
        assert exc.value.reason == reason


def test_invalid_limit_wrong_type_at_codec_boundary(repo) -> None:
    from app.pagination import validate_limit

    with pytest.raises(InvalidPaginationParamError) as exc:
        validate_limit(True)
    assert exc.value.reason == "limit_wrong_type"
    with pytest.raises(InvalidPaginationParamError):
        validate_limit("5")  # type: ignore[arg-type]


def test_malformed_cursors_rejected(repo) -> None:
    for bad, reason in [
        ("", "cursor_empty"),
        ("!!!not-base64!!!", "cursor_malformed_base64"),
        ("aGVsbG8", "cursor_bad_json"),  # 解码为 "hello"
        ("eyJpZCI6IjEifQ", "cursor_bad_payload"),  # {"id":"1"} 缺 ts
    ]:
        logger.info("invalid-cursor input token=%r expected reason=%s", bad, reason)
        with pytest.raises(InvalidCursorError) as exc:
            repo.list_page(10, bad)
        assert exc.value.reason == reason


def test_stale_cursor_when_anchor_deleted(repo, database) -> None:
    with database.conn() as conn:
        _seed_direct(conn, [("a1", "2026-01-01T00:00:01+00:00", "t1"), ("a2", "2026-01-01T00:00:02+00:00", "t2")], manage_tx=True)

    token = encode_cursor("2026-01-01T00:00:02+00:00", "a2")
    with database.transaction() as conn:
        conn.execute("DELETE FROM articles WHERE id = 'a2'")

    logger.info("stale-cursor input token=%s decision=anchor missing -> STALE not INVALID", token)
    with pytest.raises(StaleCursorError) as exc:
        repo.list_page(10, token)
    assert exc.value.reason == "cursor_anchor_deleted"


def test_cursor_roundtrip_codec() -> None:
    token = encode_cursor("2026-01-01T00:00:00+00:00", "id-1")
    cursor = decode_cursor(token)
    assert cursor is not None
    assert cursor.created_at == "2026-01-01T00:00:00+00:00"
    assert cursor.id == "id-1"


def test_create_with_repo_fixture_logs(repo, caplog) -> None:
    """补充验证：正常写入走 repository 后，created_at 可用于排序。"""
    repo.create(ArticleCreate(id="c1", title="hello", content="body"))
    page = repo.list_page(10)
    assert [a.id for a in page.items] == ["c1"]
    assert any("list_page input" in r.message for r in caplog.records)
