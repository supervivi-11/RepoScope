from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.ingestion import (
    GithubClient,
    GithubIssue,
    IngestionService,
    RepositoryMetadata,
    RepositorySnapshot,
    SafeArchiveExtractor,
    SnapshotCleaner,
    SnapshotScopeError,
    UnsupportedRepositoryLanguageError,
    UnsafeArchiveError,
)

SHA = "b" * 40
NOW = datetime(2026, 8, 29, 8, 15, tzinfo=UTC)


def _archive_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _github_handler(archive_bytes: bytes, *, language: str | None = "Python"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/openai/codex":
            return httpx.Response(
                200,
                json={
                    "name": "codex",
                    "owner": {"login": "openai"},
                    "default_branch": "main",
                    "size": 100,
                    "private": False,
                    "language": language,
                    "html_url": "https://github.com/openai/codex",
                },
            )
        if request.url.path == "/repos/openai/codex/issues/7":
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "title": "Parser fails on aliases",
                    "body": "Reproduction details",
                    "state": "open",
                    "html_url": "https://github.com/openai/codex/issues/7",
                },
            )
        if request.url.path == "/repos/openai/codex/commits/main":
            return httpx.Response(
                200,
                json={
                    "sha": SHA,
                    "html_url": f"https://github.com/openai/codex/commit/{SHA}",
                    "commit": {
                        "message": "Current head",
                        "committer": {"date": "2026-08-28T12:30:00Z"},
                    },
                },
            )
        if request.url.path == "/repos/openai/codex/commits":
            assert request.url.params["sha"] == SHA
            return httpx.Response(200, json=[])
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"total_count": 0, "items": []})
        if request.url.host == "codeload.github.com":
            assert request.url.path == f"/openai/codex/zip/{SHA}"
            return httpx.Response(200, content=archive_bytes)
        raise AssertionError(f"Unexpected request: {request.url}")

    return handler


@pytest.mark.anyio
async def test_ingestion_service_builds_immutable_snapshot_from_resolved_sha(
    tmp_path,
) -> None:
    """Breaks if metadata and extracted source do not share one immutable commit."""
    source = b"def parse_alias():\n    return True\n"
    archive = _archive_bytes(
        {
            f"codex-{SHA}/src/parser.py": source,
            f"codex-{SHA}/asset.bin": b"ignored",
        }
    )
    settings = Settings(snapshot_root=tmp_path / "snapshots")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_github_handler(archive))
    ) as http:
        service = IngestionService.from_settings(
            GithubClient(http), SafeArchiveExtractor(), settings, clock=lambda: NOW
        )
        result = await service.ingest("https://github.com/openai/codex.git/", 7)

    assert result.issue == GithubIssue(
        number=7,
        title="Parser fails on aliases",
        body="Reproduction details",
        state="open",
        html_url="https://github.com/openai/codex/issues/7",
    )
    assert result.repository == RepositoryMetadata(
        owner="openai",
        repository="codex",
        default_branch="main",
        size_kb=100,
        html_url="https://github.com/openai/codex",
        language="Python",
    )
    assert result.recent_commits == ()
    assert result.related_issues == ()
    assert result.snapshot.owner == "openai"
    assert result.snapshot.repository == "codex"
    assert result.snapshot.commit_sha == SHA
    assert result.snapshot.indexed_byte_count == len(source)
    assert result.snapshot.cleanup_deadline == NOW + timedelta(hours=24)
    assert result.snapshot.root_path.is_relative_to((tmp_path / "snapshots").resolve())
    assert (result.snapshot.root_path / "src/parser.py").read_bytes() == source


@pytest.mark.anyio
async def test_ingestion_service_removes_partial_snapshot_when_archive_is_unsafe(
    tmp_path,
) -> None:
    """Breaks if a failed extraction leaves partially trusted source on disk."""
    archive = _archive_bytes({f"codex-{SHA}/../escape.py": b"unsafe\n"})
    snapshot_root = tmp_path / "snapshots"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_github_handler(archive))
    ) as http:
        service = IngestionService(
            GithubClient(http),
            SafeArchiveExtractor(),
            snapshot_root=snapshot_root,
            clock=lambda: NOW,
        )
        with pytest.raises(UnsafeArchiveError):
            await service.ingest("https://github.com/openai/codex", 7)

    assert list(snapshot_root.iterdir()) == []
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.anyio
async def test_ingestion_rejects_snapshot_without_retained_python_source(tmp_path) -> None:
    """Breaks if a repository label can substitute for actual retained Python source."""
    archive = _archive_bytes({f"codex-{SHA}/README.md": b"# No Python here\n"})
    snapshot_root = tmp_path / "snapshots"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_github_handler(archive, language="Python"))
    ) as http:
        service = IngestionService(
            GithubClient(http),
            SafeArchiveExtractor(),
            snapshot_root=snapshot_root,
            clock=lambda: NOW,
        )
        with pytest.raises(UnsupportedRepositoryLanguageError):
            await service.ingest("https://github.com/openai/codex", 7)

    assert list(snapshot_root.iterdir()) == []


@pytest.mark.anyio
async def test_ingestion_accepts_polyglot_repository_with_retained_python_source(
    tmp_path,
) -> None:
    """Breaks if GitHub primary-language metadata wrongly rejects Python source."""
    archive = _archive_bytes(
        {
            f"codex-{SHA}/src/parser.py": b"def parse():\n    return True\n",
            f"codex-{SHA}/web/app.js": b"ignored",
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_github_handler(archive, language="JavaScript"))
    ) as http:
        result = await IngestionService(
            GithubClient(http),
            SafeArchiveExtractor(),
            snapshot_root=tmp_path / "snapshots",
            clock=lambda: NOW,
        ).ingest("https://github.com/openai/codex", 7)

    assert result.repository.language == "JavaScript"
    assert (result.snapshot.root_path / "src/parser.py").is_file()


@pytest.mark.anyio
async def test_ingestion_accepts_snapshot_with_python_stub_as_only_python_source(
    tmp_path,
) -> None:
    """Breaks if `.pyi` stubs stop satisfying the Python-only source contract."""
    archive = _archive_bytes(
        {f"codex-{SHA}/src/parser.pyi": b"def parse() -> bool: ...\n"}
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_github_handler(archive, language="C"))
    ) as http:
        result = await IngestionService(
            GithubClient(http),
            SafeArchiveExtractor(),
            snapshot_root=tmp_path / "snapshots",
            clock=lambda: NOW,
        ).ingest("https://github.com/openai/codex", 7)

    assert (result.snapshot.root_path / "src/parser.pyi").is_file()


def _snapshot(path, *, deadline: datetime) -> RepositorySnapshot:
    return RepositorySnapshot.create(
        owner="openai",
        repository="codex",
        commit_sha=SHA,
        root_path=path,
        indexed_byte_count=1,
        created_at=deadline - timedelta(hours=24),
    )


def test_snapshot_cleaner_deletes_only_expired_snapshots_inside_configured_root(
    tmp_path,
) -> None:
    """Breaks if cleanup removes live snapshots or ignores their cleanup deadline."""
    root = tmp_path / "snapshots"
    expired_path = root / "expired"
    live_path = root / "live"
    expired_path.mkdir(parents=True)
    live_path.mkdir()
    (expired_path / "source.py").write_text("old\n", encoding="utf-8")
    (live_path / "source.py").write_text("live\n", encoding="utf-8")
    expired = _snapshot(expired_path, deadline=NOW - timedelta(seconds=1))
    live = _snapshot(live_path, deadline=NOW + timedelta(seconds=1))

    removed = SnapshotCleaner(root).cleanup_expired([expired, live], now=NOW)

    assert removed == (expired_path.resolve(),)
    assert not expired_path.exists()
    assert live_path.exists()


@pytest.mark.parametrize("outside_kind", ["sibling", "root"])
def test_snapshot_cleaner_rejects_paths_outside_a_snapshot_descendant(
    tmp_path, outside_kind: str
) -> None:
    """Breaks if cleanup can delete the configured root or any neighboring directory."""
    root = tmp_path / "snapshots"
    root.mkdir()
    candidate = root if outside_kind == "root" else tmp_path / "sibling"
    if candidate != root:
        candidate.mkdir()
    (candidate / "keep.txt").write_text("keep\n", encoding="utf-8")
    snapshot = _snapshot(candidate, deadline=NOW - timedelta(seconds=1))

    with pytest.raises(SnapshotScopeError):
        SnapshotCleaner(root).cleanup_expired([snapshot], now=NOW)

    assert (candidate / "keep.txt").exists()
