"""并发更新冲突测试（本地文件 SQLite + 线程即可复现）。

场景：
1. 两个调用方都基于 version=1 更新同一条记录 -> 恰好一方成功，
   另一方明确失败（VERSION_CONFLICT，携带 expected/actual），无静默覆盖；
2. 串行复现“丢失更新”：陈旧 expected_version 必须被拒绝；
3. 写锁被持有时第三方的更新得到 INFRA_UNAVAILABLE（业务冲突 vs 基础设施错误可区分）。
"""

from __future__ import annotations

import logging
import threading

import pytest

from app.errors import InfraUnavailableError, VersionConflictError
from app.models import ArticleCreate, ArticleUpdate

logger = logging.getLogger("tests.concurrency")


def _make_article(repo) -> str:
    repo.create(ArticleCreate(id="doc1", title="v1-title", content="v1-body"))
    return "doc1"


def test_optimistic_concurrent_updates_one_wins_one_conflicts(repo) -> None:
    _make_article(repo)

    # 双方都读到 version=1（模拟各自在本地持有旧版本），同时发起更新。
    alice = ArticleUpdate(id="doc1", expected_version=1, title="alice")
    bob = ArticleUpdate(id="doc1", expected_version=1, content="bob")

    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def worker(name: str, payload: ArticleUpdate) -> None:
        barrier.wait(timeout=5)
        try:
            outcomes[name] = repo.update(payload)
        except Exception as exc:  # noqa: BLE001
            outcomes[name] = exc

    t1 = threading.Thread(target=worker, args=("alice", alice))
    t2 = threading.Thread(target=worker, args=("bob", bob))
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)

    logger.info(
        "concurrent input both expected_version=1; alice=%r bob=%r",
        type(outcomes["alice"]).__name__,
        type(outcomes["bob"]).__name__,
    )

    # SQLite 单写者 + busy_timeout 使两个写事务串行提交：
    # 先提交者成功并把版本推进到 2；后提交者的 WHERE version=1 匹配 0 行 -> 冲突。
    winners = [n for n, v in outcomes.items() if not isinstance(v, Exception)]
    losers = [n for n, v in outcomes.items() if isinstance(v, VersionConflictError)]
    assert winners == ["alice"] or winners == ["bob"], outcomes
    assert len(losers) == 1, outcomes

    conflict: VersionConflictError = outcomes[losers[0]]  # type: ignore[assignment]
    logger.info(
        "conflict decision loser=%s code=%s reason=%s details=%s",
        losers[0],
        conflict.code.value,
        conflict.reason,
        conflict.details,
    )
    assert conflict.details["expected_version"] == 1
    assert conflict.details["actual_version"] == 2

    page = repo.list_page(10)
    final = page.items[0]
    assert final.version == 2
    # 失败者的写入不得发生：只可能是 alice 赢（title 改、content 保留）或 bob 赢（反之）。
    if winners[0] == "bob":
        assert final.content == "bob"
        assert final.title == "v1-title"
    else:
        assert final.title == "alice"
        assert final.content == "v1-body"


def test_stale_update_is_rejected_sequential(repo) -> None:
    _make_article(repo)
    first = repo.update(ArticleUpdate(id="doc1", expected_version=1, title="after-first"))
    assert first.version == 2

    logger.info("stale input expected_version=1 but current=2 decision=must reject")
    with pytest.raises(VersionConflictError) as exc:
        repo.update(ArticleUpdate(id="doc1", expected_version=1, title="stale-write"))
    assert exc.value.code.value == "VERSION_CONFLICT"
    assert exc.value.details["actual_version"] == 2

    final = repo.list_page(10).items[0]
    assert final.title == "after-first"


def test_busy_lock_is_infra_not_business_conflict(repo, database) -> None:
    _make_article(repo)

    lock_taken = threading.Event()
    done = threading.Event()

    def holder() -> None:
        conn = database._new_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            lock_taken.set()
            done.wait(timeout=5)
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    t = threading.Thread(target=holder)
    t.start()
    try:
        lock_taken.wait(timeout=5)
        # timeout=0：拿不到写锁立即报 SQLITE_BUSY -> INFRA_UNAVAILABLE。
        fast_db = type(database)(database.database, timeout=0)
        fast_repo = type(repo)(fast_db, clock=repo._clock)
        logger.info("busy input lock-held-by-other timeout=0 decision=expect INFRA_UNAVAILABLE(503)")
        with pytest.raises(InfraUnavailableError) as exc:
            fast_repo.update(ArticleUpdate(id="doc1", expected_version=1, title="x"))
        logger.info("busy decision code=%s http=%s", exc.value.code.value, exc.value.http_status)
        assert exc.value.http_status == 503
    finally:
        done.set()
        t.join(timeout=5)
