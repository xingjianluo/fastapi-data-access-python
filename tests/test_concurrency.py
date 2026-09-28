"""并发更新冲突测试：乐观锁保证不出现后写入者静默覆盖。"""

from __future__ import annotations

import threading

import pytest

from app.errors import (
    UniqueConstraintError,
    VersionConflictError,
)
from app.repository import RecordRepository
from app.storage import InMemoryTable


def test_concurrent_updates_only_one_succeeds(log_case):
    """N 个线程持同一初始版本并发更新：恰有一个成功，其余明确冲突失败。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    record = repo.create("shared", 0)

    n_threads = 16
    barrier = threading.Barrier(n_threads)
    outcomes: list[tuple[int, str, int | None]] = []
    lock = threading.Lock()

    def worker(worker_id: int) -> None:
        barrier.wait()  # 尽可能放大竞争
        try:
            updated = repo.update(
                record.id, name=None, score=worker_id,
                expected_version=record.version,
            )
            result = ("ok", updated.version)
        except VersionConflictError as exc:
            result = ("conflict", exc.reason == "version_conflict")
        except Exception as exc:  # pragma: no cover - 不允许出现其他异常
            result = ("other", type(exc).__name__)
        with lock:
            outcomes.append((worker_id, result[0], result[1]))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [o for o in outcomes if o[1] == "ok"]
    conflicts = [o for o in outcomes if o[1] == "conflict"]
    final = repo.get(record.id)

    verdict = (
        "成功者数量=%d（要求恰为1），冲突失败者=%d，最终版本=%d，最终score等于成功者写入=%s"
        % (len(ok), len(conflicts), final.version,
           final.score == [o[0] for o in ok][0] if ok else False)
    )
    log_case(
        "concurrent_updates_same_version",
        {"threads": n_threads, "record_id": record.id,
         "expected_version": record.version},
        verdict,
        extra={"final": {"version": final.version, "score": final.score},
               "ok_workers": [o[0] for o in ok]},
    )
    assert len(ok) == 1
    assert len(conflicts) == n_threads - 1
    assert final.version == 2  # 只成功过一次
    assert final.score == ok[0][0]


def test_stale_update_after_someone_else_committed(log_case):
    """串行化模拟丢失更新：B 基于旧版本的修改必须被拒绝。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    record = repo.create("doc", 1)

    # A 先提交，版本推进到 2
    repo.update(record.id, name=None, score=11, expected_version=1)
    # B 仍持版本 1 提交
    with pytest.raises(VersionConflictError) as exc_info:
        repo.update(record.id, name=None, score=99, expected_version=1)

    final = repo.get(record.id)
    log_case(
        "stale_update_rejected",
        {"expected_version": 1, "current_version": 2, "b_wanted_score": 99},
        "拒绝原因=%s，A 的写入被保留(score=%d)，B 未静默覆盖=%s"
        % (exc_info.value.reason, final.score, final.score == 11),
    )
    assert exc_info.value.reason == "version_conflict"
    assert final.score == 11
    assert final.version == 2


def test_concurrent_create_same_name_one_rejected(log_case):
    """并发创建同名记录：唯一约束只放行一个，另一个明确归类为 unique_conflict。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            repo.create("dup-name", 1)
            tag = "ok"
        except UniqueConstraintError:
            tag = "unique_conflict"
        with lock:
            outcomes.append(tag)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_case(
        "concurrent_create_same_name",
        {"threads": n_threads, "name": "dup-name"},
        "成功=%d，唯一冲突=%d" % (outcomes.count("ok"),
                                    outcomes.count("unique_conflict")),
        extra={"outcomes": sorted(outcomes)},
    )
    assert outcomes.count("ok") == 1
    assert outcomes.count("unique_conflict") == n_threads - 1


def test_delete_with_stale_version_rejected(log_case):
    """带旧版本删除同样走乐观锁，不能误删已被修改的记录。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    record = repo.create("doc", 1)
    repo.update(record.id, name=None, score=2, expected_version=1)

    with pytest.raises(VersionConflictError) as exc_info:
        repo.delete(record.id, expected_version=1)

    log_case(
        "stale_delete_rejected",
        {"expected_version": 1, "current_version": 2},
        "拒绝原因=%s，记录仍存在=%s" % (
            exc_info.value.reason, repo.get(record.id).version == 2),
    )
    assert repo.get(record.id).version == 2


def test_concurrent_disjoint_records_all_succeed(log_case):
    """竞争不同记录互不阻塞：各自更新各自成功，验证冲突只针对同一行。"""
    table = InMemoryTable()
    repo = RecordRepository(table)
    records = [repo.create(f"r{i}", 0) for i in range(10)]
    errors: list[Exception] = []

    def worker(idx: int, rec_version: int) -> None:
        try:
            repo.update(records[idx].id, name=None, score=idx + 100,
                        expected_version=rec_version)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i, records[i].version))
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    versions = {r.id: repo.get(r.id).version for r in records}
    log_case(
        "concurrent_disjoint_records",
        {"records": 10},
        "异常数=%d（要求0），全部版本推进到2=%s"
        % (len(errors), all(v == 2 for v in versions.values())),
    )
    assert errors == []
    assert all(v == 2 for v in versions.values())
