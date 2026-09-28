"""SQLite 连接管理。

使用本地文件型 SQLite + WAL，保证并发读写测试可复现。
每次操作使用独立连接（check_same_thread=False），写事务用 BEGIN IMMEDIATE
尽早获取写锁，配合 busy_timeout 减少 SQLITE_BUSY。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.errors import InfraInternalError, InfraStorageFullError, InfraUnavailableError

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# SQLite 扩展错误码（标准 sqlite3 模块未导出常量）
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_SQLITE_READONLY = 8
_SQLITE_FULL = 13
_SQLITE_IOERR = 26
_SQLITE_BUSY_SNAPSHOT = 520


def translate_sqlite_error(exc: sqlite3.Error) -> Exception:
    """把底层 sqlite3 异常翻译为可对外暴露的基础设施错误。

    绝不把原始异常（含 SQL/路径信息）直接抛出。
    """
    code = getattr(exc, "sqlite_errorcode", None)
    message = str(exc).lower()
    if code in (_SQLITE_BUSY, _SQLITE_BUSY_SNAPSHOT, _SQLITE_LOCKED) or "locked" in message:
        return InfraUnavailableError(reason="SQLITE_BUSY: write lock held by another transaction")
    if code == _SQLITE_FULL or "disk is full" in message or "database or disk is full" in message:
        return InfraStorageFullError(reason="SQLITE_FULL: no space left for database")
    if code == _SQLITE_IOERR or "i/o error" in message:
        return InfraStorageFullError(reason="SQLITE_IOERR: storage medium unavailable")
    return InfraInternalError(reason=f"sqlite3.{exc.__class__.__name__}")


def connect(database: str = "data/app.db", *, timeout: float = 5.0) -> sqlite3.Connection:
    """建立一个配置好行工厂与外键的连接。"""
    conn = sqlite3.connect(
        database,
        timeout=timeout,
        check_same_thread=False,
        isolation_level=None,  # 手动事务（BEGIN IMMEDIATE / COMMIT）
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if timeout > 0:
        # 单位毫秒：等待锁的时间，超时后才抛 SQLITE_BUSY。
        conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    return conn


def init_db(database: str = "data/app.db", *, timeout: float = 5.0) -> None:
    """初始化表结构；文件型数据库启用 WAL。"""
    conn = connect(database, timeout=timeout)
    try:
        if not database.startswith("file:") and database != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


class Database:
    """连接持有者：封装连接串与连接工厂。"""

    def __init__(self, database: str = "data/app.db", *, timeout: float = 5.0) -> None:
        self.timeout = timeout
        if database == ":memory:":
            # 共享缓存内存库：所有连接看到同一份数据，需保持一个长连接存活。
            self.database = "file:daal_mem?mode=memory&cache=shared&uri=true"
            # 供 create_app/init_db 等按“内存库”语义使用的标识。
            self.target = ":memory:"
            self._uri_mode = True
        else:
            self.database = database
            self.target = database
            self._uri_mode = False
        self._keeper: sqlite3.Connection | None = None

    def _new_conn(self) -> sqlite3.Connection:
        if self._uri_mode:
            conn = sqlite3.connect(
                self.database,
                uri=True,
                timeout=self.timeout,
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if self.timeout > 0:
                conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
            return conn
        return connect(self.database, timeout=self.timeout)

    def init(self) -> None:
        if self._uri_mode:
            # 共享缓存内存库需要至少一个常驻连接，否则数据随最后一个连接关闭而消失；
            # 直接在 keeper 上建表，不再走文件型 init_db。
            self._keeper = self._new_conn()
            self._keeper.executescript(SCHEMA_SQL)
            return
        init_db(self.database, timeout=self.timeout)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """获取短连接，自动关闭；底层异常统一翻译。"""
        conn = self._new_conn()
        try:
            yield conn
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from None
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式写事务：BEGIN IMMEDIATE，成功提交，任何异常都回滚并翻译。

        BEGIN IMMEDIATE 在事务起点即申请 RESERVED 写锁，把锁等待集中到一处，
        避免读-判断-写窗口内的锁竞争被延迟到 COMMIT 才暴露。
        """
        conn = self._new_conn()
        begun = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            begun = True
            yield conn
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if begun:
                conn.execute("ROLLBACK")
            raise translate_sqlite_error(exc) from None
        except BaseException:
            # AppError / KeyboardInterrupt 等：回滚后原样上抛，不做翻译。
            if begun:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
