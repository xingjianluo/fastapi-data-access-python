"""统一错误分类。

所有数据访问错误最终映射为 AppError，避免把底层异常直接透给调用方。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """对外错误码：业务/参数类错误 vs 基础设施类错误严格区分。"""

    # —— 参数 / 业务校验（4xx，调用方可修正）——
    INVALID_PAGINATION_PARAM = "INVALID_PAGINATION_PARAM"
    INVALID_CURSOR = "INVALID_CURSOR"
    STALE_CURSOR = "STALE_CURSOR"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RESOURCE_ALREADY_EXISTS = "RESOURCE_ALREADY_EXISTS"
    BATCH_ABORTED = "BATCH_ABORTED"

    # —— 基础设施（5xx，调用方无法通过修改请求解决）——
    INFRA_UNAVAILABLE = "INFRA_UNAVAILABLE"
    INFRA_STORAGE_FULL = "INFRA_STORAGE_FULL"
    INFRA_DEADLINE = "INFRA_DEADLINE"
    INFRA_INTERNAL = "INFRA_INTERNAL"


class AppError(Exception):
    """应用错误基类。"""

    def __init__(self, code: ErrorCode, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.details: dict[str, object] = {}


class BusinessError(AppError):
    """业务/参数错误（4xx）。"""

    http_status = 400


class InfraError(AppError):
    """基础设施错误（5xx）。"""

    http_status = 500


class InvalidPaginationParamError(BusinessError):
    http_status = 422

    def __init__(self, reason: str, message: str = "非法分页参数") -> None:
        super().__init__(ErrorCode.INVALID_PAGINATION_PARAM, message, reason=reason)


class InvalidCursorError(BusinessError):
    http_status = 422

    def __init__(self, reason: str, message: str = "分页游标无法识别") -> None:
        super().__init__(ErrorCode.INVALID_CURSOR, message, reason=reason)


class StaleCursorError(BusinessError):
    http_status = 409

    def __init__(self, reason: str, message: str = "分页游标已失效") -> None:
        super().__init__(ErrorCode.STALE_CURSOR, message, reason=reason)


class VersionConflictError(BusinessError):
    http_status = 409

    def __init__(self, resource_id: str, expected: int, actual: int | None) -> None:
        super().__init__(
            ErrorCode.VERSION_CONFLICT,
            "记录已被其他事务修改，版本不匹配",
            reason="expected_version != current_version",
        )
        self.details = {"id": resource_id, "expected_version": expected, "actual_version": actual}


class RecordNotFoundError(BusinessError):
    http_status = 404

    def __init__(self, resource_id: str) -> None:
        super().__init__(
            ErrorCode.RECORD_NOT_FOUND,
            "记录不存在",
            reason="no row with given id",
        )
        self.details = {"id": resource_id}


class ValidationFailedError(BusinessError):
    http_status = 422

    def __init__(self, reason: str, field: str = "") -> None:
        super().__init__(ErrorCode.VALIDATION_FAILED, "业务校验未通过", reason=reason)
        if field:
            self.details = {"field": field}


class ResourceAlreadyExistsError(BusinessError):
    http_status = 409

    def __init__(self, field: str, value: str) -> None:
        super().__init__(
            ErrorCode.RESOURCE_ALREADY_EXISTS,
            "唯一约束冲突",
            reason="unique constraint violated",
        )
        self.details = {"field": field, "value": value}


class BatchAbortedError(BusinessError):
    http_status = 409

    def __init__(self, index: int, cause: AppError) -> None:
        super().__init__(
            ErrorCode.BATCH_ABORTED,
            f"批量写操作在第 {index} 步失败，整体已回滚",
            reason=f"rolled back due to: {cause.code.value}",
        )
        self.details = {"failed_index": index, "cause_code": cause.code.value, "cause_message": cause.message}


class InfraUnavailableError(InfraError):
    http_status = 503

    def __init__(self, reason: str = "database locked/busy") -> None:
        super().__init__(ErrorCode.INFRA_UNAVAILABLE, "存储当前不可用，请稍后重试", reason=reason)


class InfraStorageFullError(InfraError):
    http_status = 507

    def __init__(self, reason: str = "database disk full") -> None:
        super().__init__(ErrorCode.INFRA_STORAGE_FULL, "存储资源已满", reason=reason)


class InfraInternalError(InfraError):
    http_status = 500

    def __init__(self, reason: str) -> None:
        super().__init__(ErrorCode.INFRA_INTERNAL, "数据访问内部错误", reason=reason)


def register_exception_handlers(app) -> None:  # noqa: ANN001
    """把 AppError 统一映射为结构化 JSON 响应；未预期异常兜底为 INFRA_INTERNAL。

    底层异常（如 sqlite3.Error）不允许直接透给调用方：
    Repository 必须先翻译；若仍有漏网的，统一折叠为 500/INFRA_INTERNAL。
    """
    from fastapi.responses import JSONResponse

    @app.exception_handler(AppError)
    async def _handle_app_error(_request, exc: AppError) -> JSONResponse:
        status = getattr(exc, "http_status", 500)
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": exc.code.value,
                    "category": "BUSINESS" if isinstance(exc, BusinessError) else "INFRASTRUCTURE",
                    "message": exc.message,
                    "reason": exc.reason,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unknown(_request, exc: Exception) -> JSONResponse:
        # 不向调用方泄露内部信息（SQL、文件路径等），仅返回稳定错误体。
        internal = InfraInternalError(reason=exc.__class__.__name__)
        return JSONResponse(
            status_code=internal.http_status,
            content={
                "error": {
                    "code": internal.code.value,
                    "category": "INFRASTRUCTURE",
                    "message": internal.message,
                    "reason": internal.reason,
                    "details": {},
                }
            },
        )

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": ErrorCode.VALIDATION_FAILED.value,
                    "category": "BUSINESS",
                    "message": "请求参数校验未通过",
                    "reason": "request schema validation failed",
                    "details": {"errors": exc.errors()},
                }
            },
        )
