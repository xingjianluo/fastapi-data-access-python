"""领域模型与 API DTO。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Article:
    id: str
    title: str
    content: str
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ArticleCreate:
    id: str
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class ArticleUpdate:
    id: str
    expected_version: int
    title: str | None = None
    content: str | None = None


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class Page:
    items: list[Article]
    next_cursor: str | None
