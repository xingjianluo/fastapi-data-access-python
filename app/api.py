"""HTTP 接口层：参数解析、错误分类映射、响应组织。

错误分类：
- 4xx 结构化业务错误：invalid_pagination / version_conflict /
  unique_conflict / record_not_found / business_validation
- 5xx 基础设施错误：storage_full(507) / infrastructure_error(503)
任何存储/运行期底层异常都会被兜底转换，不把堆栈或内部文本透给调用方。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import AppError, InfrastructureError
from app.pagination import MAX_LIMIT, validate_page_params
from app.repository import RecordRepository
from app.service import RecordService
from app.storage import InMemoryTable

logger = logging.getLogger("app.api")


def create_app(max_records: int = 1000) -> FastAPI:
    app = FastAPI(
        title="Data Access Layer Demo",
        version="0.1.0",
    )
    table = InMemoryTable(max_records=max_records)
    service = RecordService(RecordRepository(table))

    # ---- 错误处理 ----
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # 基础设施错误记 error 日志（含内部细节），业务冲突记 info
        if isinstance(exc, InfrastructureError):
            logger.error("基础设施错误 path=%s reason=%s",
                         request.url.path, exc.reason)
        else:
            logger.info("业务拒绝 path=%s reason=%s msg=%s",
                        request.url.path, exc.reason, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content={"ok": False, "error": exc.to_dict()},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：内部细节只进日志，不透传给调用方
        logger.exception("未预期错误 path=%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {
                    "error_type": "internal_error",
                    "reason": "internal_error",
                    "message": "服务内部错误，请稍后重试",
                },
            },
        )

    # ---- 路由 ----
    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/records")
    def list_records(
        sort_by: str = "created_at",
        order: str = "asc",
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict:
        query = validate_page_params(sort_by, order, limit, cursor)
        result = service.list_page(query)
        result["ok"] = True
        return result

    @app.post("/records", status_code=201)
    def create_record(payload: dict) -> dict:
        return {"ok": True, "record": service.create_record(
            payload.get("name"), payload.get("score")
        )}

    @app.get("/records/{record_id}")
    def get_record(record_id: int) -> dict:
        return {"ok": True, "record": service.get_record(record_id)}

    @app.put("/records/{record_id}")
    def update_record(record_id: int, payload: dict) -> dict:
        return {"ok": True, "record": service.update_record(record_id, payload)}

    @app.delete("/records/{record_id}")
    def delete_record(record_id: int, payload: dict) -> JSONResponse:
        service.delete_record(record_id, payload)
        return JSONResponse(status_code=204, content=None)

    @app.post("/records/transaction")
    def transaction(payload: dict) -> dict:
        if not isinstance(payload, dict) or "ops" not in payload:
            from app.errors import BusinessValidationError

            raise BusinessValidationError(
                "请求体必须包含 ops 数组", reason="missing_ops"
            )
        results = service.run_transaction(payload["ops"])
        return {"ok": True, "results": results}

    return app
