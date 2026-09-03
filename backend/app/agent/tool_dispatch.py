from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.investigation import InvestigationTools, SourceExcerpt
from app.investigation.tools import _normalize_relative_path

from .models import EvidenceCitation, ToolRequest, ToolResultSummary


ALLOWED_TOOL_NAMES = (
    "find_references",
    "find_symbol",
    "get_recent_commits",
    "get_related_issues",
    "get_repository_map",
    "read_code",
    "search_code",
)


class _ToolArguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GetRepositoryMapArguments(_ToolArguments):
    pass


class SearchCodeArguments(_ToolArguments):
    query: str = Field(max_length=500)
    path_prefix: str | None = None

    @field_validator("query")
    @classmethod
    def _nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must be nonblank")
        return value

    @field_validator("path_prefix")
    @classmethod
    def _normalized_optional_prefix(cls, value: str | None) -> str | None:
        return (
            _normalize_relative_path(value, allow_trailing=True)
            if value is not None
            else None
        )


class ReadCodeArguments(_ToolArguments):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def _normalized_path(cls, value: str) -> str:
        return _normalize_relative_path(value)

    @field_validator("end_line")
    @classmethod
    def _bounded_end_line(cls, value: int) -> int:
        return value

    def model_post_init(self, __context: Any) -> None:
        if self.end_line < self.start_line or self.end_line - self.start_line + 1 > 200:
            raise ValueError("invalid source line range")


class FindSymbolArguments(_ToolArguments):
    symbol: str = Field(min_length=1, max_length=500)

    @field_validator("symbol")
    @classmethod
    def _nonblank_symbol(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("symbol must be nonblank")
        return value


class FindReferencesArguments(FindSymbolArguments):
    pass


class GetRecentCommitsArguments(_ToolArguments):
    path: str | None = None

    @field_validator("path")
    @classmethod
    def _normalized_optional_path(cls, value: str | None) -> str | None:
        return _normalize_relative_path(value) if value is not None else None


class GetRelatedIssuesArguments(_ToolArguments):
    query: str = Field(max_length=500)

    @field_validator("query")
    @classmethod
    def _nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must be nonblank")
        return value


_ARGUMENT_MODELS: dict[str, type[_ToolArguments]] = {
    "find_references": FindReferencesArguments,
    "find_symbol": FindSymbolArguments,
    "get_recent_commits": GetRecentCommitsArguments,
    "get_related_issues": GetRelatedIssuesArguments,
    "get_repository_map": GetRepositoryMapArguments,
    "read_code": ReadCodeArguments,
    "search_code": SearchCodeArguments,
}


async def dispatch_tool_request(
    tools: InvestigationTools, request: ToolRequest
) -> ToolResultSummary:
    tool_name = request.tool_name or ""
    argument_model = _ARGUMENT_MODELS.get(tool_name)
    if argument_model is None:
        return _failed(tool_name, "Unsupported investigation tool.")
    try:
        arguments = argument_model.model_validate(request.arguments_dict())
    except (ValidationError, ValueError):
        return _failed(tool_name, "Invalid investigation tool arguments.")

    try:
        method = getattr(tools, tool_name)
        result = await asyncio.to_thread(method, **arguments.model_dump())
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        return ToolResultSummary(
            tool_name=tool_name,
            arguments_summary=_arguments_summary(tool_name, arguments),
            succeeded=False,
            safe_error="Tool execution failed safely.",
        )

    citations = _extract_citations(result)
    return ToolResultSummary(
        tool_name=tool_name,
        arguments_summary=_arguments_summary(tool_name, arguments),
        result_summary=_result_summary(result, citations),
        citations=citations,
        succeeded=True,
    )


def _failed(tool_name: str, safe_error: str) -> ToolResultSummary:
    return ToolResultSummary(
        tool_name=tool_name if tool_name in ALLOWED_TOOL_NAMES else "unknown",
        arguments_summary="rejected",
        succeeded=False,
        safe_error=safe_error,
    )


def _arguments_summary(tool_name: str, arguments: _ToolArguments) -> str:
    values = arguments.model_dump()
    if tool_name in {"search_code", "get_related_issues"}:
        query = values.pop("query")
        values["query_length"] = len(query)
    return ", ".join(f"{key}={values[key]}" for key in sorted(values))


def _extract_citations(result: Any) -> tuple[EvidenceCitation, ...]:
    found: list[EvidenceCitation] = []

    def visit(value: Any) -> None:
        if isinstance(value, SourceExcerpt):
            found.append(
                EvidenceCitation(
                    commit_sha=value.location.commit_sha,
                    path=value.location.path,
                    start_line=value.location.start_line,
                    end_line=value.location.end_line,
                    excerpt=value.excerpt,
                    explanation="Exact source returned by an investigation tool.",
                )
            )
            return
        if isinstance(value, BaseModel):
            for field_name in type(value).model_fields:
                visit(getattr(value, field_name))
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                visit(item)

    visit(result)
    unique: dict[tuple[str, str, int, int, str], EvidenceCitation] = {}
    for item in found:
        key = (
            item.commit_sha,
            item.path,
            item.start_line,
            item.end_line,
            item.excerpt,
        )
        unique.setdefault(key, item)
    return tuple(unique[key] for key in sorted(unique))[:32]


def _result_summary(
    result: Any, citations: tuple[EvidenceCitation, ...]
) -> str:
    # Model-facing observations are distinct from redacted public tool events.
    # Source text already lives in citations: retain navigation metadata here.
    for limit in (40, 20, 10, 5, 2, 1):
        truncated = False

        def project(value: Any, depth: int = 0) -> Any:
            nonlocal truncated
            if depth > 6:
                truncated = True
                return None
            if isinstance(value, SourceExcerpt):
                return value.location.model_dump(mode="json")
            if isinstance(value, BaseModel):
                return {
                    name: project(getattr(value, name), depth + 1)
                    for name in type(value).model_fields
                }
            if isinstance(value, (tuple, list)):
                truncated |= len(value) > limit
                return [project(item, depth + 1) for item in value[:limit]]
            if isinstance(value, str):
                maximum = min(1000, limit * 100)
                truncated |= len(value) > maximum
                return value[:maximum]
            if isinstance(value, datetime):
                return value.isoformat()
            if value is None or type(value) in {bool, int, float}:
                return value
            return type(value).__name__

        data = project(result)
        encoded = json.dumps({
            "type": type(result).__name__, "untrusted_data": True,
            "data": data, "citations": len(citations), "truncated": truncated,
        }, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) <= 32768:
            return encoded
    return json.dumps({"type": type(result).__name__, "untrusted_data": True,
                       "data": None, "truncated": True})
