"""数据访问层：把存储原语组织成可复用的领域操作。

列表使用键集分页：where (sort_value, id) >/< (cursor_value, cursor_id)，
游标同时携带排序值与主键，排序值重复时靠 id 决胜，保证不重不漏。
更新/删除带版本前提（乐观锁）；多写操作通过一次 commit 原子提交。
"""

from __future__ import annotations

from app.models import Record, StagedChange
from app.pagination import (
    PageQuery,
    PageResult,
    encode_cursor,
)
from app.storage import InMemoryTable


class RecordRepository:
    def __init__(self, table: InMemoryTable) -> None:
        self.table = table

    def list_page(self, query: PageQuery) -> PageResult:
        rows = self.table.snapshot()

        def key(r: Record) -> tuple[int, int]:
            return (getattr(r, query.sort_by), r.id)

        if query.cursor_sort_value is not None:
            assert query.cursor_id is not None
            cursor_key = (query.cursor_sort_value, query.cursor_id)
            if query.order == "asc":
                rows = [r for r in rows if key(r) > cursor_key]
            else:
                rows = [r for r in rows if key(r) < cursor_key]

        rows.sort(key=key, reverse=(query.order == "desc"))

        has_more = len(rows) > query.limit
        page = rows[: query.limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(
                query.sort_by, query.order,
                getattr(last, query.sort_by), last.id,
            )
        return PageResult(items=page, next_cursor=next_cursor, has_more=has_more)

    def get(self, record_id: int) -> Record:
        from app.errors import RecordNotFoundError

        row = self.table.get(record_id)
        if row is None:
            raise RecordNotFoundError(
                f"记录 {record_id} 不存在", reason="record_not_found"
            )
        return row

    def create(self, name: str, score: int) -> Record:
        (row,) = self.table.commit([StagedChange("create", None, name, score)])
        return row  # type: ignore[return-value]

    def update(
        self, record_id: int, name: str | None, score: int | None,
        expected_version: int,
    ) -> Record:
        (row,) = self.table.commit(
            [StagedChange("update", record_id, name, score, expected_version)]
        )
        return row  # type: ignore[return-value]

    def delete(self, record_id: int, expected_version: int) -> None:
        self.table.commit(
            [StagedChange("delete", record_id, None, None, expected_version)]
        )

    def commit_many(self, changes: list[StagedChange]) -> list[Record | None]:
        return self.table.commit(changes)
