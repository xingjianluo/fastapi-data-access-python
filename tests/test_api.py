"""HTTP 接口集成测试：错误码、HTTP 状态、错误体结构端到端可区分。"""

from __future__ import annotations

import logging

logger = logging.getLogger("tests.api")


def test_root(client) -> None:
    assert client.get("/").json() == {"Hello": "World"}


def test_create_list_and_paginate(client) -> None:
    for i in range(5):
        r = client.post("/articles", json={"id": f"n{i}", "title": f"title-{i}", "content": "x"})
        assert r.status_code == 201, r.text
        assert r.json()["version"] == 1

    r1 = client.get("/articles", params={"limit": 2})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["has_more"] is True
    assert body1["next_cursor"]
    logger.info("api page1 input limit=2 items=%s next=%s", [i["id"] for i in body1["items"]], body1["next_cursor"])

    r2 = client.get("/articles", params={"limit": 2, "cursor": body1["next_cursor"]})
    assert r2.status_code == 200
    ids1 = [i["id"] for i in body1["items"]]
    ids2 = [i["id"] for i in r2.json()["items"]]
    assert not (set(ids1) & set(ids2)), "相邻页不得有重复项"


def test_invalid_limit_http_errors(client) -> None:
    for bad, reason in [("0", "limit_not_positive"), ("-1", "limit_not_positive"), ("999", "limit_too_large")]:
        r = client.get("/articles", params={"limit": bad})
        logger.info("api invalid-limit input limit=%s status=%s", bad, r.status_code)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_PAGINATION_PARAM"
        assert r.json()["error"]["reason"] == reason
        assert r.json()["error"]["category"] == "BUSINESS"


def test_invalid_and_stale_cursor_http(client) -> None:
    client.post("/articles", json={"id": "x1", "title": "t", "content": "c"})

    r = client.get("/articles", params={"cursor": "!!!not-base64!!!"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_CURSOR"
    assert r.json()["error"]["reason"] == "cursor_malformed_base64"

    # 合法编码但锚点不存在 -> 409 STALE_CURSOR，与 422 INVALID_CURSOR 可区分。
    import base64, json
    token = base64.urlsafe_b64encode(json.dumps({"ts": "2020-01-01T00:00:00+00:00", "id": "ghost"}).encode()).decode().rstrip("=")
    r2 = client.get("/articles", params={"cursor": token})
    logger.info("api stale cursor status=%s body=%s", r2.status_code, r2.json()["error"]["code"])
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "STALE_CURSOR"


def test_concurrent_update_conflict_over_http(client) -> None:
    client.post("/articles", json={"id": "e1", "title": "orig", "content": "orig"})

    ok = client.put("/articles/e1", json={"expected_version": 1, "title": "first-write"})
    assert ok.status_code == 200
    assert ok.json()["version"] == 2

    stale = client.put("/articles/e1", json={"expected_version": 1, "title": "lost-write"})
    logger.info("api conflict status=%s body=%s", stale.status_code, stale.json())
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "VERSION_CONFLICT"
    assert err["category"] == "BUSINESS"
    assert err["details"]["expected_version"] == 1
    assert err["details"]["actual_version"] == 2

    current = client.get("/articles").json()["items"][0]
    assert current["title"] == "first-write"


def test_batch_rollback_over_http(client) -> None:
    payload = {
        "operations": [
            {"action": "create", "id": "g1", "title": "ok", "content": ""},
            {"action": "update", "id": "g1", "expected_version": 5, "title": "bad-version"},
        ]
    }
    r = client.post("/articles/batch", json=payload)
    logger.info("api batch rollback status=%s", r.status_code)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "BATCH_ABORTED"
    assert err["details"]["failed_index"] == 1
    assert err["details"]["cause_code"] == "VERSION_CONFLICT"

    items = client.get("/articles").json()["items"]
    assert all(i["id"] != "g1" for i in items), "回滚后 g1 不得存在"


def test_batch_success_commits_all(client) -> None:
    payload = {
        "operations": [
            {"action": "create", "id": "h1", "title": "a", "content": ""},
            {"action": "create", "id": "h2", "title": "b", "content": ""},
        ]
    }
    r = client.post("/articles/batch", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 2


def test_request_schema_validation_is_business(client) -> None:
    r = client.post("/articles", json={"id": "", "title": ""})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "BUSINESS"
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_not_found_category(client) -> None:
    r = client.put("/articles/missing", json={"expected_version": 1, "title": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RECORD_NOT_FOUND"
