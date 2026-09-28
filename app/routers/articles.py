"""文章资源 HTTP 路由。

路由层只负责：请求/响应模型、调用 Repository、DTO 序列化。
所有数据访问异常由 app.errors 的处理器统一分类返回。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from app.models import Article, ArticleCreate, ArticleUpdate
from app.repository import ArticleRepository

router = APIRouter(prefix="/articles", tags=["articles"])


# 依赖注入：由 create_app 通过 dependency_overrides 或闭包提供实现。
_repo_holder: dict[str, ArticleRepository] = {}


def set_repository(repo: ArticleRepository) -> None:
    _repo_holder["repo"] = repo


def get_repository() -> ArticleRepository:
    repo = _repo_holder.get("repo")
    if repo is None:  # pragma: no cover - 应用工厂必定注入
        raise RuntimeError("repository not configured")
    return repo


RepoDep = Annotated[ArticleRepository, Depends(get_repository)]


# --------------------------------------------------------------------- DTO
class CreateIn(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(default="")


class UpdateIn(BaseModel):
    expected_version: int = Field(gt=0)
    title: str | None = None
    content: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "UpdateIn":
        if self.title is None and self.content is None:
            raise ValueError("title 与 content 至少提供一个")
        if self.title is not None and not self.title.strip():
            raise ValueError("title 不能为空白")
        return self


class CreateOp(BaseModel):
    action: Literal["create"]
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(default="")


class UpdateOp(BaseModel):
    action: Literal["update"]
    id: str = Field(min_length=1)
    expected_version: int = Field(gt=0)
    title: str | None = None
    content: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "UpdateOp":
        if self.title is None and self.content is None:
            raise ValueError("title 与 content 至少提供一个")
        if self.title is not None and not self.title.strip():
            raise ValueError("title 不能为空白")
        return self


class BatchIn(BaseModel):
    operations: list[Annotated[CreateOp | UpdateOp, Field(discriminator="action")]] = Field(min_length=1)


def _serialize(article: Article) -> dict[str, Any]:
    return {
        "id": article.id,
        "title": article.title,
        "content": article.content,
        "version": article.version,
        "created_at": article.created_at,
        "updated_at": article.updated_at,
    }


# ------------------------------------------------------------------- 路由
@router.get("")
def list_articles(
    repo: RepoDep,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    page = repo.list_page(limit, cursor)
    return {
        "items": [_serialize(a) for a in page.items],
        "next_cursor": page.next_cursor,
        "has_more": page.next_cursor is not None,
    }


@router.post("", status_code=201)
def create_article(body: CreateIn, repo: RepoDep) -> dict[str, Any]:
    article = repo.create(ArticleCreate(id=body.id, title=body.title, content=body.content))
    return _serialize(article)


@router.put("/{article_id}")
def update_article(article_id: str, body: UpdateIn, repo: RepoDep) -> dict[str, Any]:
    article = repo.update(
        ArticleUpdate(
            id=article_id,
            expected_version=body.expected_version,
            title=body.title,
            content=body.content,
        )
    )
    return _serialize(article)


@router.post("/batch", status_code=201)
def batch_articles(body: BatchIn, repo: RepoDep) -> dict[str, Any]:
    ops: list[ArticleCreate | ArticleUpdate] = []
    for op in body.operations:
        if op.action == "create":
            ops.append(ArticleCreate(id=op.id, title=op.title, content=op.content))
        else:
            ops.append(
                ArticleUpdate(
                    id=op.id,
                    expected_version=op.expected_version,
                    title=op.title,
                    content=op.content,
                )
            )
    created = repo.batch(ops)
    return {"items": [_serialize(a) for a in created], "count": len(created)}
