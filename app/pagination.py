"""键集分页（keyset pagination）：游标编解码与参数校验。

游标内同时携带排序字段值与记录 id，用于在排序值重复时做稳定的
确定性比较；对“新数据写入 / 数据删除 / 排序值重复”天然不重不漏。

游标是不透明字符串，编码内容为 base64url(JSON)，其中：
    s : 生成游标时使用的排序字段
    o : 生成游标时使用的方向 asc/desc
    v : 最后一条记录的排序值
    i : 最后一条记录的 id（并列时的决胜键）
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from app.errors import PaginationError
from app.models import SORT_FIELDS

MAX_LIMIT = 100
ORDERS = ("asc", "desc")


@dataclass(frozen=True, slots=True)
class PageQuery:
    sort_by: str
    order: str            # asc / desc
    limit: int
    cursor_sort_value: int | None = None
    cursor_id: int | None = None


@dataclass(frozen=True, slots=True)
class CursorData:
    sort_by: str
    order: str
    sort_value: int
    record_id: int


@dataclass(frozen=True, slots=True)
class PageResult:
    items: list
    next_cursor: str | None
    has_more: bool


def encode_cursor(sort_by: str, order: str, sort_value: int, record_id: int) -> str:
    payload = {"s": sort_by, "o": order, "v": sort_value, "i": record_id}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> CursorData:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise PaginationError(
            "游标格式非法，无法解码", reason="cursor_malformed"
        ) from exc

    if not isinstance(payload, dict) or {"s", "o", "v", "i"} - payload.keys():
        raise PaginationError(
            "游标内容不完整，缺少必要字段", reason="cursor_malformed"
        )
    sort_by, order, sort_value, record_id = (
        payload["s"], payload["o"], payload["v"], payload["i"],
    )
    if (
        not isinstance(sort_by, str)
        or not isinstance(order, str)
        # bool 是 int 的子类，显式排除
        or isinstance(sort_value, bool)
        or isinstance(record_id, bool)
        or not isinstance(sort_value, int)
        or not isinstance(record_id, int)
    ):
        raise PaginationError(
            "游标字段类型不正确", reason="cursor_malformed"
        )
    return CursorData(sort_by, order, sort_value, record_id)


def validate_page_params(
    sort_by: str, order: str, limit: int, cursor: str | None
) -> PageQuery:
    """校验分页参数，非法时抛出带可区分 reason 的 PaginationError。"""
    if sort_by not in SORT_FIELDS:
        raise PaginationError(
            f"不支持的排序字段: {sort_by!r}，允许 {SORT_FIELDS}",
            reason="invalid_sort_by",
        )
    if order not in ORDERS:
        raise PaginationError(
            f"不支持的排序方向: {order!r}，允许 {ORDERS}",
            reason="invalid_order",
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise PaginationError("limit 必须是整数", reason="limit_not_integer")
    if limit < 1:
        raise PaginationError("limit 必须 >= 1", reason="limit_too_small")
    if limit > MAX_LIMIT:
        raise PaginationError(
            f"limit 不能超过 {MAX_LIMIT}", reason="limit_too_large"
        )

    cursor_value: int | None = None
    cursor_id: int | None = None
    if cursor is not None:
        data = decode_cursor(cursor)
        if data.sort_by != sort_by:
            raise PaginationError(
                "游标与当前排序字段不一致，该游标已失效",
                reason="cursor_sort_mismatch",
            )
        if data.order != order:
            raise PaginationError(
                "游标与当前排序方向不一致，该游标已失效",
                reason="cursor_order_mismatch",
            )
        cursor_value, cursor_id = data.sort_value, data.record_id

    return PageQuery(sort_by, order, limit, cursor_value, cursor_id)
