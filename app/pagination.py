"""键集（keyset）分页。

排序键固定为 (created_at DESC, id DESC)，保证全序：
- 新数据写入不会改变旧行的相对位置，不会漏项；
- created_at 重复时由 id 打破并列，不会重复；
- 删除行只会让结果少一行，不会翻出重复数据。

游标为不透明 token：base64url(JSON({"ts":..., "id":...}))。
游标无法解码 -> INVALID_CURSOR；游标格式合法但锚点行已不存在 -> STALE_CURSOR。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from app.errors import InvalidCursorError, InvalidPaginationParamError

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: str
    id: str


def validate_limit(raw: int | None) -> int:
    """校验分页大小。

    拒绝原因（可区分）：
    - limit_wrong_type: 不是整数；
    - limit_not_positive: <= 0；
    - limit_too_large: 超过 MAX_LIMIT。
    """
    if raw is None:
        return DEFAULT_LIMIT
    # FastAPI 通常在进入此函数前已保证 int，bool 是 int 的子类需单独排除。
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise InvalidPaginationParamError(reason="limit_wrong_type")
    if raw <= 0:
        raise InvalidPaginationParamError(reason="limit_not_positive")
    if raw > MAX_LIMIT:
        raise InvalidPaginationParamError(
            reason="limit_too_large",
            message=f"分页大小超过上限 {MAX_LIMIT}",
        )
    return raw


def encode_cursor(created_at: str, article_id: str) -> str:
    """把排序锚点编码为不透明游标。"""
    payload = json.dumps({"ts": created_at, "id": article_id}, separators=(",", ":"), ensure_ascii=False)
    token = base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=")
    return token.decode("ascii")


def decode_cursor(token: str | None) -> Cursor | None:
    """解码并校验游标；无法识别时抛 InvalidCursorError 并给出具体 reason。

    拒绝原因：
    - cursor_not_string / cursor_empty；
    - cursor_malformed_base64；
    - cursor_bad_json；
    - cursor_bad_payload：字段缺失或类型错误。
    """
    if token is None:
        return None
    if not isinstance(token, str):
        raise InvalidCursorError(reason="cursor_not_string")
    if token == "":
        raise InvalidCursorError(reason="cursor_empty")
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise InvalidCursorError(reason="cursor_malformed_base64") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError(reason="cursor_bad_json") from exc
    if not isinstance(payload, dict):
        raise InvalidCursorError(reason="cursor_bad_payload")
    ts = payload.get("ts")
    article_id = payload.get("id")
    if not isinstance(ts, str) or not ts or not isinstance(article_id, str) or not article_id:
        raise InvalidCursorError(reason="cursor_bad_payload")
    return Cursor(created_at=ts, id=article_id)


def page_where(cursor: Cursor | None) -> tuple[str, tuple[object, ...]]:
    """根据游标生成 WHERE 片段与绑定参数。

    DESC 排序下，下一页取“严格位于锚点之后”的行：
        created_at < ts  或  created_at = ts 且 id < anchor_id
    严格不等保证锚点行自身不会在相邻两页重复出现。
    """
    if cursor is None:
        return "", ()
    return (
        "WHERE created_at < ? OR (created_at = ? AND id < ?)",
        (cursor.created_at, cursor.created_at, cursor.id),
    )
