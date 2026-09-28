"""数据访问层（Repository）。

职责：
- 键集分页查询（稳定排序 + 不透明游标，翻页期间写入/删除不重不漏）；
- 基于 version 的乐观并发更新（后写者不会静默覆盖先写者）；
- 单事务批量写入（任一步失败整体回滚）；
- 把 sqlite3 底层异常翻译为 app.errors 中的分类错误，不透传给调用方。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from app.db import Database
from app.errors import (
    RecordNotFoundError,
    ResourceAlreadyExistsError,
    StaleCursorError,
    ValidationFailedError,
    VersionConflictError,
)
from app.models import Article, ArticleCreate, ArticleUpdate, Page
from app.pagination import decode_cursor, encode_cursor, page_where, validate_limit

logger = logging.getLogger("app.data")

_COLUMNS = "id, title, content, version, created_at, updated_at"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_article(row: sqlite3.Row) -> Article:
    return Article(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ArticleRepository:
    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db = db
        self._clock = clock or _utc_now_iso

    # ------------------------------------------------------------------ 查询
    def list_page(self, limit: int, cursor_token: str | None = None) -> Page:
        """键集分页查询。

        判定依据：
        - limit 经 validate_limit 校验；
        - cursor 经 decode_cursor 解码，无法识别即拒绝；
        - 游标锚点行必须仍存在，否则判定游标失效（可能被删除或属于旧数据集）。
        """
        page_size = validate_limit(limit)
        cursor = decode_cursor(cursor_token)
        logger.info(
            "list_page input limit=%s cursor_token=%s decoded=%s",
            limit,
            cursor_token,
            cursor,
        )

        with self.db.conn() as conn:
            if cursor is not None:
                anchor = conn.execute(
                    "SELECT 1 FROM articles WHERE created_at = ? AND id = ?",
                    (cursor.created_at, cursor.id),
                ).fetchone()
                if anchor is None:
                    logger.info(
                        "cursor rejected as STALE: anchor(ts=%s,id=%s) not found in current dataset",
                        cursor.created_at,
                        cursor.id,
                    )
                    raise StaleCursorError(reason="cursor_anchor_deleted")

            where_sql, where_params = page_where(cursor)
            sql = (
                f"SELECT {_COLUMNS} FROM articles {where_sql} "
                "ORDER BY created_at DESC, id DESC LIMIT ?"
            )
            rows = conn.execute(sql, (*where_params, page_size + 1)).fetchall()

        has_more = len(rows) > page_size
        items = [_row_to_article(r) for r in rows[:page_size]]
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
        logger.info(
            "list_page result returned=%d has_more=%s next_cursor_present=%s",
            len(items),
            has_more,
            next_cursor is not None,
        )
        return Page(items=items, next_cursor=next_cursor)

    # ------------------------------------------------------------------ 校验
    @staticmethod
    def _validate_create(data: ArticleCreate) -> None:
        if not isinstance(data.id, str) or not data.id.strip():
            raise ValidationFailedError(reason="id_required", field="id")
        if not isinstance(data.title, str) or not data.title.strip():
            raise ValidationFailedError(reason="title_required_non_blank", field="title")
        if not isinstance(data.content, str):
            raise ValidationFailedError(reason="content_required_string", field="content")

    @staticmethod
    def _validate_update(data: ArticleUpdate) -> None:
        if not isinstance(data.id, str) or not data.id.strip():
            raise ValidationFailedError(reason="id_required", field="id")
        if not isinstance(data.expected_version, int) or isinstance(data.expected_version, bool):
            raise ValidationFailedError(reason="expected_version_required_integer", field="expected_version")
        if data.expected_version <= 0:
            raise ValidationFailedError(reason="expected_version_must_be_positive", field="expected_version")
        if data.title is None and data.content is None:
            raise ValidationFailedError(reason="update_requires_at_least_one_field")
        if data.title is not None and not data.title.strip():
            raise ValidationFailedError(reason="title_required_non_blank", field="title")

    # ------------------------------------------------------------------ 写入
    def _create_on_conn(self, conn: sqlite3.Connection, data: ArticleCreate) -> Article:
        self._validate_create(data)
        now = self._clock()
        try:
            conn.execute(
                "INSERT INTO articles (id, title, content, version, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (data.id, data.title, data.content, now, now),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if message.startswith("UNIQUE"):
                raise ResourceAlreadyExistsError(field="id", value=data.id) from None
            raise ValidationFailedError(reason=f"integrity:{message.split(':')[0]}") from exc
        row = conn.execute(f"SELECT {_COLUMNS} FROM articles WHERE id = ?", (data.id,)).fetchone()
        logger.info("create input id=%s title=%r -> version=1", data.id, data.title)
        return _row_to_article(row)

    def create(self, data: ArticleCreate, *, conn: sqlite3.Connection | None = None) -> Article:
        """插入一条记录；唯一冲突时抛 ResourceAlreadyExistsError。"""
        if conn is not None:
            return self._create_on_conn(conn, data)
        with self.db.transaction() as tx:
            return self._create_on_conn(tx, data)

    def _update_on_conn(self, conn: sqlite3.Connection, data: ArticleUpdate) -> Article:
        self._validate_update(data)
        now = self._clock()
        sets = ["version = version + 1", "updated_at = ?"]
        params: list[object] = [now]
        if data.title is not None:
            sets.append("title = ?")
            params.append(data.title)
        if data.content is not None:
            sets.append("content = ?")
            params.append(data.content)
        params.extend([data.id, data.expected_version])

        logger.info(
            "update input id=%s expected_version=%s fields=%s",
            data.id,
            data.expected_version,
            [k for k in ("title", "content") if getattr(data, k) is not None],
        )
        cur = conn.execute(
            f"UPDATE articles SET {', '.join(sets)} WHERE id = ? AND version = ?",
            params,
        )
        if cur.rowcount == 0:
            current = conn.execute(
                "SELECT version FROM articles WHERE id = ?", (data.id,)
            ).fetchone()
            if current is None:
                logger.info("update conflict id=%s reason=record_not_found", data.id)
                raise RecordNotFoundError(data.id)
            logger.info(
                "update conflict id=%s expected_version=%s actual_version=%s reason=version_mismatch",
                data.id,
                data.expected_version,
                current["version"],
            )
            raise VersionConflictError(data.id, data.expected_version, current["version"])
        row = conn.execute(f"SELECT {_COLUMNS} FROM articles WHERE id = ?", (data.id,)).fetchone()
        article = _row_to_article(row)
        logger.info(
            "update accepted id=%s new_version=%s decision=expected_version matched",
            article.id,
            article.version,
        )
        return article

    def update(self, data: ArticleUpdate, *, conn: sqlite3.Connection | None = None) -> Article:
        """乐观更新；版本不匹配抛 VersionConflictError，记录不存在抛 RecordNotFoundError。"""
        if conn is not None:
            return self._update_on_conn(conn, data)
        with self.db.transaction() as tx:
            return self._update_on_conn(tx, data)

    def batch(self, ops: Sequence[ArticleCreate | ArticleUpdate]) -> list[Article]:
        """在单个事务内顺序执行多个写操作，任一业务步骤失败整体回滚。

        - 业务校验/版本冲突 -> BATCH_ABORTED（4xx），事务回滚；
        - 锁/磁盘等基础设施错误 -> 保持 INFRA_*（5xx）透传分类，同样回滚；
        两类错误不会互相混淆。
        """
        if not ops:
            raise ValidationFailedError(reason="batch_requires_at_least_one_op")
        logger.info("batch input size=%d", len(ops))
        results: list[Article] = []
        with self.db.transaction() as conn:
            for index, op in enumerate(ops):
                try:
                    if isinstance(op, ArticleCreate):
                        results.append(self._create_on_conn(conn, op))
                    elif isinstance(op, ArticleUpdate):
                        results.append(self._update_on_conn(conn, op))
                    else:  # pragma: no cover - 类型系统已限制，仅作运行时兜底
                        raise ValidationFailedError(reason="unsupported_op_type")
                except Exception as exc:
                    from app.errors import AppError, BatchAbortedError, BusinessError

                    if isinstance(exc, BusinessError):
                        logger.info(
                            "batch aborted at index=%d due to business error code=%s; rollback issued",
                            index,
                            exc.code.value,
                        )
                        raise BatchAbortedError(index, exc) from None
                    if isinstance(exc, AppError):
                        logger.info(
                            "batch aborted at index=%d due to infra error code=%s; rollback issued",
                            index,
                            exc.code.value,
                        )
                        raise
                    raise
        logger.info("batch committed size=%d", len(results))
        return results
