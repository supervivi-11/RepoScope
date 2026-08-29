from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agent import ALLOWED_TOOL_NAMES, ToolRequest, dispatch_tool_request
from app.ingestion import RepositorySnapshot
from app.investigation import InvestigationTools, PythonRepositoryIndex


SHA = "a" * 40


class _GithubFake:
    async def fetch_recent_commits(self, *args, **kwargs):
        return ()

    async def fetch_related_issues(self, *args, **kwargs):
        return ()


def _tools(tmp_path: Path) -> InvestigationTools:
    root = tmp_path / "snapshot"
    path = root / "src" / "parser.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def parse(value):\n    return value\n", encoding="utf-8", newline=""
    )
    snapshot = RepositorySnapshot.create(
        owner="octo",
        repository="demo",
        commit_sha=SHA,
        root_path=root,
        indexed_byte_count=path.stat().st_size,
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    return InvestigationTools(PythonRepositoryIndex.build(snapshot), _GithubFake())


def test_dispatch_allowlist_is_exactly_the_task_three_surface() -> None:
    """Breaks if the agent gains shell, write, or arbitrary-network capabilities."""
    assert ALLOWED_TOOL_NAMES == (
        "find_references",
        "find_symbol",
        "get_recent_commits",
        "get_related_issues",
        "get_repository_map",
        "read_code",
        "search_code",
    )


@pytest.mark.anyio
async def test_unsupported_tool_and_invalid_arguments_fail_without_invocation(
    tmp_path: Path,
) -> None:
    """Breaks if model-selected names or arguments bypass explicit schemas."""
    tools = _tools(tmp_path)

    unsupported = await dispatch_tool_request(
        tools, ToolRequest(tool_name="run_shell", arguments={"command": "env"})
    )
    invalid = await dispatch_tool_request(
        tools,
        ToolRequest(
            tool_name="read_code",
            arguments={"path": "src/parser.py", "start_line": 0, "end_line": 2},
        ),
    )
    invalid_prefix = await dispatch_tool_request(
        tools,
        ToolRequest(
            tool_name="search_code",
            arguments={"query": "parser", "path_prefix": "../outside"},
        ),
    )

    assert unsupported.succeeded is False
    assert unsupported.safe_error == "Unsupported investigation tool."
    assert invalid.succeeded is False
    assert invalid.safe_error == "Invalid investigation tool arguments."
    assert invalid_prefix.safe_error == "Invalid investigation tool arguments."
    assert unsupported.result_summary is None
    assert invalid.result_summary is None
    assert invalid_prefix.result_summary is None


@pytest.mark.anyio
async def test_exact_read_is_summarized_with_deterministic_evidence(tmp_path: Path) -> None:
    """Breaks if real tool evidence loses commit, path, range, or exact excerpt."""
    result = await dispatch_tool_request(
        _tools(tmp_path),
        ToolRequest(
            tool_name="read_code",
            arguments={"path": "src/parser.py", "start_line": 1, "end_line": 2},
        ),
    )

    assert result.succeeded is True
    assert result.tool_name == "read_code"
    assert result.arguments_summary == "end_line=2, path=src/parser.py, start_line=1"
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.commit_sha == SHA
    assert citation.path == "src/parser.py"
    assert (citation.start_line, citation.end_line) == (1, 2)
    assert citation.excerpt == "def parse(value):\n    return value\n"


@pytest.mark.anyio
async def test_tool_exception_becomes_a_generic_safe_failure(tmp_path: Path) -> None:
    """Breaks if repository paths, stack traces, or exception text leak to state."""
    result = await dispatch_tool_request(
        _tools(tmp_path),
        ToolRequest(
            tool_name="read_code",
            arguments={"path": "missing.py", "start_line": 1, "end_line": 1},
        ),
    )

    assert result.succeeded is False
    assert result.safe_error == "Tool execution failed safely."
    serialized = result.model_dump_json()
    assert "Traceback" not in serialized
    assert "snapshot" not in serialized
