"""HTTP 层测试：错误响应结构、错误分类不透传底层异常、翻页接口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app


@pytest.fixture
def client():
    return TestClient(create_app(max_records=3), raise_server_exceptions=False)


def test_error_envelope_and_distinct_reasons(client, log_case):
    """各类非法分页参数返回统一信封，reason 可区分。"""
    cases = [
        ("/records?limit=0", 400, "limit_too_small"),
        ("/records?limit=101", 400, "limit_too_large"),
        ("/records?sort_by=hacker", 400, "invalid_sort_by"),
        ("/records?order=down", 400, "invalid_order"),
        ("/records?cursor=%21%21%21", 400, "cursor_malformed"),
    ]
    for url, status, reason in cases:
        r = client.get(url)
        body = r.json()
        ok = (
            r.status_code == status
            and body["ok"] is False
            and set(body["error"]) == {"error_type", "reason", "message"}
            and body["error"]["reason"] == reason
        )
        log_case("api_error_envelope", {"url": url},
                 f"status={r.status_code} reason={body['error']['reason']} 匹配={ok}")
        assert ok, body


def test_version_conflict_over_http(client, log_case):
    created = client.post("/records", json={"name": "a", "score": 1}).json()
    vid = created["record"]["id"]
    client.put(f"/records/{vid}", json={"score": 2, "expected_version": 1})
    r = client.put(f"/records/{vid}", json={"score": 3, "expected_version": 1})
    body = r.json()
    log_case(
        "api_version_conflict",
        {"id": vid, "stale_expected_version": 1},
        "status=%d error_type=%s reason=%s"
        % (r.status_code, body["error"]["error_type"], body["error"]["reason"]),
    )
    assert r.status_code == 409
    assert body["error"]["error_type"] == "version_conflict"


def test_transaction_http_rollback_and_full(client, log_case):
    client.post("/records", json={"name": "a", "score": 1})

    # 唯一冲突 -> 409 + 事务回滚包装
    r1 = client.post("/records/transaction", json={"ops": [
        {"op": "create", "name": "b", "score": 1},
        {"op": "create", "name": "a", "score": 1},
    ]})
    b1 = r1.json()["error"]

    # 容量满 -> 507 基础设施
    r2 = client.post("/records/transaction", json={"ops": [
        {"op": "create", "name": "b", "score": 1},
        {"op": "create", "name": "c", "score": 1},
        {"op": "create", "name": "d", "score": 1},
    ]})
    b2 = r2.json()["error"]

    log_case(
        "api_tx_error_classification",
        {"case1": "unique_conflict", "case2": "storage_full"},
        "case1: status=%d cause=%s | case2: status=%d cause=%s（业务4xx vs 基础设施5xx）"
        % (r1.status_code, b1["cause_type"], r2.status_code, b2["cause_type"]),
    )
    assert r1.status_code == 409
    assert b1["error_type"] == "transaction_aborted"
    assert b1["cause_type"] == "unique_conflict"
    assert r2.status_code == 507
    assert b2["cause_type"] == "storage_full"
    # 两次失败事务均未留下数据
    names = [x["name"] for x in client.get("/records?limit=100").json()["items"]]
    assert names == ["a"]


def test_pagination_end_to_end_with_ties(log_case):
    """接口层翻页：重复 score + 翻页中间写入，整遍不重不漏。

    该用例独立使用大容量实例，避免容量夹具干扰翻页判定。
    """
    client = TestClient(create_app(max_records=100), raise_server_exceptions=False)
    for i in range(1, 6):
        client.post("/records", json={"name": f"r{i}", "score": 10})

    seen: list[int] = []
    cursor = None
    page_no = 0
    while True:
        url = f"/records?sort_by=score&order=asc&limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        page = client.get(url).json()
        ids = [x["id"] for x in page["items"]]
        seen.extend(ids)
        page_no += 1
        if page_no == 1:
            # 翻页中间写入一条新数据
            client.post("/records", json={"name": "new", "score": 10})
        if not page["next_cursor"]:
            break
        cursor = page["next_cursor"]

    verdict = (
        "无重复=%s，原始5条无遗漏=%s"
        % (len(seen) == len(set(seen)),
           all(i in seen for i in range(1, 6)))
    )
    log_case(
        "api_paging_with_ties_and_insert",
        {"initial": 5, "limit": 2, "insert_after_page": 1},
        verdict,
        extra={"seen_ids": seen, "pages": page_no},
    )
    assert len(seen) == len(set(seen))
    assert all(i in seen for i in range(1, 6))


def test_not_found_and_invalid_version(client, log_case):
    r = client.get("/records/999")
    assert r.status_code == 404

    created = client.post("/records", json={"name": "a", "score": 1}).json()
    rid = created["record"]["id"]
    r = client.put(f"/records/{rid}", json={"score": 5, "expected_version": 0})
    body = r.json()
    log_case(
        "api_invalid_version",
        {"expected_version": 0},
        "status=%d reason=%s（业务校验，非冲突）"
        % (r.status_code, body["error"]["reason"]),
    )
    assert r.status_code == 422
    assert body["error"]["reason"] == "invalid_version"
