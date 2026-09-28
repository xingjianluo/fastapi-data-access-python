"""统一错误分类。

所有抛出到调用方的异常都必须是 AppError 的子类，
框架层负责把它们映射为结构化错误响应，不向调用方透传底层异常。
"""

from __future__ import annotations


class AppError(Exception):
    """业务可识别错误的基类。"""

    error_type: str = "app_error"
    http_status: int = 400

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason or self.error_type

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type,
            "reason": self.reason,
            "message": self.message,
        }


# ---- 分页参数类 ----
class PaginationError(AppError):
    error_type = "invalid_pagination"
    http_status = 400


# ---- 并发 / 资源类 ----
class VersionConflictError(AppError):
    error_type = "version_conflict"
    http_status = 409


class RecordNotFoundError(AppError):
    error_type = "record_not_found"
    http_status = 404


class BusinessValidationError(AppError):
    """字段级业务校验失败（名称非法、分数非法等）。"""

    error_type = "business_validation"
    http_status = 422


class UniqueConstraintError(AppError):
    """唯一约束冲突，属于并发/业务冲突的一种明确分类。"""

    error_type = "unique_conflict"
    http_status = 409


# ---- 事务类 ----
class TransactionAbortedError(AppError):
    """事务中某一步失败，整体回滚。

    cause_type/cause_reason 保留导致回滚的原始错误分类，
    以区分业务校验失败（4xx）与基础设施失败（5xx）。
    """

    error_type = "transaction_aborted"
    http_status = 422

    def __init__(
        self, message: str, *, reason: str = "",
        cause_type: str = "", cause_reason: str = "",
        http_status: int | None = None,
    ) -> None:
        super().__init__(message, reason=reason)
        self.cause_type = cause_type
        self.cause_reason = cause_reason
        if http_status is not None:
            self.http_status = http_status

    @classmethod
    def from_cause(cls, exc: Exception) -> "TransactionAbortedError":
        if isinstance(exc, AppError):
            return cls(
                f"事务已中止并整体回滚，原因：{exc.message}",
                reason="transaction_aborted",
                cause_type=exc.error_type,
                cause_reason=exc.reason,
                http_status=exc.http_status,
            )
        # 非 AppError 的底层异常不外露细节，统一归为基础设施错误
        return cls(
            "事务已中止并整体回滚：存储基础设施异常",
            reason="transaction_aborted",
            cause_type="infrastructure_error",
            cause_reason="infrastructure_error",
            http_status=503,
        )

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["cause_type"] = self.cause_type
        data["cause_reason"] = self.cause_reason
        return data


# ---- 基础设施类 ----
class InfrastructureError(AppError):
    """基础设施错误：不应把底层异常文本直接给调用方。"""

    error_type = "infrastructure_error"
    http_status = 503


class StorageFullError(InfrastructureError):
    error_type = "storage_full"
    http_status = 507


class CursorEncodeError(AppError):
    error_type = "invalid_cursor"
    http_status = 400
