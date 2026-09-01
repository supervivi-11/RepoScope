from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.ingestion import CommitMetadata, GithubIssue, RepositoryCoordinates, RepositorySnapshot
from app.investigation import InvestigationTools, PythonRepositoryIndex


SHA = "b" * 40


def _tools(tmp_path: Path, github) -> InvestigationTools:
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "main.py").write_text(
        "def parse(value):\n    return value\n\nresult = parse('x')\n",
        encoding="utf-8",
        newline="",
    )
    snapshot = RepositorySnapshot.create(
        owner="octo",
        repository="demo",
        commit_sha=SHA,
        root_path=root,
        indexed_byte_count=(root / "main.py").stat().st_size,
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    return InvestigationTools(PythonRepositoryIndex.build(snapshot), github)


class _GithubFake:
    def __init__(self) -> None:
        self.commit_call: tuple[RepositoryCoordinates, str, str | None, int] | None = None
        self.issue_call: tuple[RepositoryCoordinates, str, int] | None = None

    async def fetch_recent_commits(
        self,
        coordinates: RepositoryCoordinates,
        *,
        sha: str,
        path: str | None = None,
        limit: int = 10,
    ) -> tuple[CommitMetadata, ...]:
        self.commit_call = (coordinates, sha, path, limit)
        return (
            CommitMetadata(
                sha="c" * 40,
                message="Fix parser",
                html_url="https://github.com/octo/demo/commit/" + "c" * 40,
                committed_at=datetime(2026, 8, 28, 12, 30, tzinfo=UTC),
            ),
        )

    async def fetch_related_issues(
        self,
        coordinates: RepositoryCoordinates,
        query: str,
        *,
        limit: int = 10,
    ) -> tuple[GithubIssue, ...]:
        self.issue_call = (coordinates, query, limit)
        return (
            GithubIssue(
                number=9,
                title="Parser regression",
                body="details",
                state="open",
                html_url="https://github.com/octo/demo/issues/9",
            ),
        )


def test_facade_exposes_exactly_seven_read_only_structured_tools(tmp_path) -> None:
    """Breaks if the agent surface gains shell/write/URL powers or raw return values."""
    tools = _tools(tmp_path, _GithubFake())

    public_methods = {
        name
        for name, member in inspect.getmembers(InvestigationTools, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public_methods == {
        "find_references",
        "find_symbol",
        "get_recent_commits",
        "get_related_issues",
        "get_repository_map",
        "read_code",
        "search_code",
    }
    results = (
        tools.get_repository_map(),
        tools.search_code("parse"),
        tools.read_code("main.py", 1, 2),
        tools.find_symbol("parse"),
        tools.find_references("parse"),
    )
    assert all(isinstance(result, BaseModel) for result in results)
    assert results[1].hits[0].source.location.commit_sha == SHA
    assert results[2].excerpt == "def parse(value):\n    return value\n"


@pytest.mark.anyio
async def test_github_tools_delegate_only_immutable_coordinates_sha_and_safe_inputs(
    tmp_path,
) -> None:
    """Breaks if remote tools can drift from the snapshot or accept escaping paths."""
    github = _GithubFake()
    tools = _tools(tmp_path, github)

    commits = await tools.get_recent_commits("src\\parser.py")
    issues = await tools.get_related_issues("parser regression")

    coordinates = RepositoryCoordinates(owner="octo", repository="demo")
    assert github.commit_call == (coordinates, SHA, "src/parser.py", 10)
    assert github.issue_call == (coordinates, "parser regression", 10)
    assert commits.commits[0].sha == "c" * 40
    assert issues.issues[0].number == 9
    assert isinstance(commits, BaseModel)
    assert isinstance(issues, BaseModel)

    with pytest.raises(ValueError):
        await tools.get_recent_commits("../outside.py")
    with pytest.raises(ValueError):
        await tools.get_related_issues("   ")
