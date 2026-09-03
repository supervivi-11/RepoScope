from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agent import ALLOWED_TOOL_NAMES, ToolRequest, dispatch_tool_request
from app.ingestion import RepositorySnapshot
from app.investigation import (
    InvestigationTools,
    PythonRepositoryIndex,
    SourceExcerpt,
    SourceLocation,
)


SHA = "a" * 40


@pytest.mark.anyio
async def test_repository_map_reaches_model_as_bounded_navigation_data(tmp_path: Path) -> None:
    result = await dispatch_tool_request(
        _tools(tmp_path), ToolRequest(tool_name="get_repository_map")
    )
    payload = json.loads(result.result_summary)
    assert "src.parser" in payload["data"]["python_modules"]
    assert "src" in payload["data"]["top_level_entries"]
    assert payload["untrusted_data"] is True
    assert len(result.result_summary.encode("utf-8")) <= 32768


@pytest.mark.anyio
async def test_search_observation_preserves_query_and_hit_locations(tmp_path: Path) -> None:
    result = await dispatch_tool_request(
        _tools(tmp_path), ToolRequest(tool_name="search_code", arguments={"query": "parse"})
    )
    payload = json.loads(result.result_summary)
    assert payload["data"]["query"] == "parse"
    assert payload["data"]["hits"][0]["source"]["path"] == "src/parser.py"
    assert "excerpt" not in payload["data"]["hits"][0]["source"]
    assert result.citations[0].excerpt


def test_large_navigation_observation_is_valid_bounded_json(tmp_path: Path) -> None:
    from app.agent.tool_dispatch import _result_summary

    repository_map = _tools(tmp_path).get_repository_map().model_copy(update={
        "python_modules": tuple("模块" * 500 + str(i) for i in range(1000)),
    })
    observation = _result_summary(repository_map, ())
    assert len(observation.encode("utf-8")) <= 32768
    assert json.loads(observation)["truncated"] is True


def test_history_metadata_is_visible_but_never_becomes_source_evidence() -> None:
    from app.agent.tool_dispatch import _extract_citations, _result_summary
    from app.investigation.domain import IssueRecord, RelatedIssuesResult

    result = RelatedIssuesResult(issues=(IssueRecord(
        number=9, title="Related symptom", body="Untrusted issue content",
        state="open", html_url="https://github.com/octo/demo/issues/9",
    ),))
    assert _extract_citations(result) == ()
    observation = json.loads(_result_summary(result, ()))
    assert observation["data"]["issues"][0]["title"] == "Related symptom"
    assert observation["untrusted_data"] is True


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
async def test_sync_tool_work_is_offloaded_so_heartbeats_can_run() -> None:
    heartbeat_ran = asyncio.Event()

    class BlockingTools:
        def get_repository_map(self):
            time.sleep(0.08)
            return ()

    async def heartbeat() -> None:
        await asyncio.sleep(0.01)
        heartbeat_ran.set()

    pulse = asyncio.create_task(heartbeat())
    result = await dispatch_tool_request(
        BlockingTools(), ToolRequest(tool_name="get_repository_map", arguments={})
    )
    assert heartbeat_ran.is_set()
    assert result.succeeded is True
    await pulse


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


@pytest.mark.anyio
async def test_paths_use_the_task_three_normalizer_before_any_facade_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if malformed paths reach a facade or valid separators stay uncanonical."""
    tools = _tools(tmp_path)
    calls: list[tuple[str, object]] = []
    original_read = tools.read_code

    def record_read(path: str, start_line: int, end_line: int):
        calls.append(("read_code", path))
        return original_read(path, start_line, end_line)

    monkeypatch.setattr(tools, "read_code", record_read)
    for path in (
        "C:/snapshot/src/parser.py",
        " src/parser.py",
        "src/parser.py ",
        "src/\x00parser.py",
        "./src/parser.py",
        "../src/parser.py",
        "/src/parser.py",
    ):
        result = await dispatch_tool_request(
            tools,
            ToolRequest(
                tool_name="read_code",
                arguments={"path": path, "start_line": 1, "end_line": 1},
            ),
        )
        assert result.safe_error == "Invalid investigation tool arguments."
    assert calls == []

    result = await dispatch_tool_request(
        tools,
        ToolRequest(
            tool_name="read_code",
            arguments={"path": "src\\parser.py", "start_line": 1, "end_line": 1},
        ),
    )

    assert result.succeeded is True
    assert calls == [("read_code", "src/parser.py")]


@pytest.mark.anyio
async def test_extracted_citations_are_deduplicated_sorted_and_capped_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if a large tool result violates the persisted citation contract."""
    tools = _tools(tmp_path)
    excerpts = tuple(
        SourceExcerpt(
            location=SourceLocation(
                path=f"src/{number:02}.py",
                start_line=1,
                end_line=1,
                commit_sha=SHA,
            ),
            excerpt=f"line {number}\n",
        )
        for number in reversed(range(40))
    )
    monkeypatch.setattr(tools, "find_symbol", lambda symbol: excerpts)

    result = await dispatch_tool_request(
        tools, ToolRequest(tool_name="find_symbol", arguments={"symbol": "parse"})
    )

    assert result.succeeded is True
    assert len(result.citations) == 32
    assert [citation.path for citation in result.citations] == [
        f"src/{number:02}.py" for number in range(32)
    ]
