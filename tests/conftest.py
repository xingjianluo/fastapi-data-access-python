"""pytest 公共夹具。

- 使用 tmp_path 下的本地文件型 SQLite（WAL），并发场景可复现；
- 提供可控时钟（tick_clock），让排序值重复/新数据写入场景确定；
- 打开日志输出，单测日志打印输入与判定依据。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.repository import ArticleRepository


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def database(db_path) -> Database:
    db = Database(db_path, timeout=5.0)
    db.init()
    return db


@pytest.fixture
def tick_clock() -> Iterator[Callable[[], str]]:
    """从固定起点递增的时钟：每调一次推进 1 秒，测试结果确定。"""
    state = {"n": 0}

    def _now() -> str:
        from datetime import UTC, datetime, timedelta

        ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=state["n"])
        state["n"] += 1
        return ts.isoformat()

    yield _now


@pytest.fixture
def repo(database, tick_clock) -> ArticleRepository:
    return ArticleRepository(database, clock=tick_clock)


@pytest.fixture
def client(database) -> Iterator[TestClient]:
    from main import create_app

    # 复用临时文件库：create_app 内的 init 对已存在库表是幂等的。
    application = create_app(database.target, timeout=5.0)
    with TestClient(application) as c:
        yield c


@pytest.fixture(autouse=True)
def _caplog_levels(caplog) -> Iterator[None]:
    caplog.set_level(logging.INFO, logger="app.data")
    yield
