from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceLocation(_FrozenModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def _ordered_range(self) -> SourceLocation:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class SourceExcerpt(_FrozenModel):
    location: SourceLocation
    excerpt: str


SymbolKind = Literal[
    "module", "class", "function", "async_function", "method", "async_method"
]


class SymbolRecord(_FrozenModel):
    name: str
    qualified_name: str
    kind: SymbolKind
    source: SourceExcerpt


class ImportRecord(_FrozenModel):
    module: str
    name: str | None = None
    alias: str | None = None
    source: SourceExcerpt


class CodeChunk(_FrozenModel):
    chunk_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: SourceExcerpt
    text: str
    language: str
    context_kind: Literal["code", "context"]


class SearchHit(_FrozenModel):
    source: SourceExcerpt
    score: int = Field(ge=0)
    match_kind: Literal[
        "lexical", "definition", "ast_reference", "text_reference"
    ]
    certainty: Literal["literal", "definition", "static_syntax", "conservative_text"]


class RepositoryMap(_FrozenModel):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    top_level_entries: tuple[str, ...]
    python_modules: tuple[str, ...]
    python_packages: tuple[str, ...]
    test_files: tuple[str, ...]
    context_files: tuple[str, ...]
    symbol_counts: tuple[tuple[str, int], ...]
    imports: tuple[ImportRecord, ...]
    parse_error_paths: tuple[str, ...]


class SearchResults(_FrozenModel):
    query: str
    hits: tuple[SearchHit, ...]


class SymbolResults(_FrozenModel):
    symbol: str
    symbols: tuple[SymbolRecord, ...]


class ReferenceResults(_FrozenModel):
    symbol: str
    hits: tuple[SearchHit, ...]


class CommitRecord(_FrozenModel):
    sha: str
    message: str
    html_url: str
    committed_at: datetime


class RecentCommitsResult(_FrozenModel):
    commits: tuple[CommitRecord, ...]


class IssueRecord(_FrozenModel):
    number: int
    title: str
    body: str | None
    state: str
    html_url: str


class RelatedIssuesResult(_FrozenModel):
    issues: tuple[IssueRecord, ...]
