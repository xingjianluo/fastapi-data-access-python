"""FastAPI 入口。"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from app.db import Database
from app.errors import register_exception_handlers
from app.repository import ArticleRepository
from app.routers import articles


def create_app(database: str = "data/app.db", *, timeout: float = 5.0) -> FastAPI:
    """应用工厂：初始化库表、注入 Repository、注册错误处理器。"""
    app = FastAPI(title="fastapi-data-access-python", version="0.1.0")

    if database != ":memory:":
        parent = os.path.dirname(database)
        if parent:
            os.makedirs(parent, exist_ok=True)

    db = Database(database, timeout=timeout)
    db.init()
    articles.set_repository(ArticleRepository(db))

    register_exception_handlers(app)
    app.include_router(articles.router)

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"Hello": "World"}

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
