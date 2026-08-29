from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .archive import SafeArchiveExtractor
from .domain import (
    CommitMetadata,
    GithubIssue,
    RepositoryCoordinates,
    RepositoryMetadata,
    RepositorySnapshot,
    validate_issue_number,
)
from .errors import SnapshotScopeError
from .github import GithubClient

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True, slots=True)
class IngestionResult:
    issue: GithubIssue
    repository: RepositoryMetadata
    recent_commits: tuple[CommitMetadata, ...]
    related_issues: tuple[GithubIssue, ...]
    snapshot: RepositorySnapshot


class IngestionService:
    def __init__(
        self,
        github: GithubClient,
        extractor: SafeArchiveExtractor,
        *,
        snapshot_root: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._github = github
        self._extractor = extractor
        self._snapshot_root = snapshot_root.resolve()
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_settings(
        cls,
        github: GithubClient,
        extractor: SafeArchiveExtractor,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> IngestionService:
        return cls(
            github,
            extractor,
            snapshot_root=settings.snapshot_root,
            clock=clock,
        )

    async def ingest(self, repository_url: str, issue_number: object) -> IngestionResult:
        coordinates = RepositoryCoordinates.parse(repository_url)
        number = validate_issue_number(issue_number)

        repository = await self._github.fetch_repository(coordinates)
        issue = await self._github.fetch_issue(coordinates, number)
        commit_sha = await self._github.fetch_default_branch_head(
            coordinates, repository.default_branch
        )
        recent_commits = await self._github.fetch_recent_commits(coordinates)
        related_issues = await self._github.fetch_related_issues(
            coordinates, issue.title
        )
        archive_bytes = await self._github.download_archive(coordinates, commit_sha)

        self._snapshot_root.mkdir(parents=True, exist_ok=True)
        snapshot_path = Path(
            tempfile.mkdtemp(
                prefix=f"{coordinates.owner}-{coordinates.repository}-{commit_sha[:12]}-",
                dir=self._snapshot_root,
            )
        ).resolve()
        try:
            extraction = self._extractor.extract(archive_bytes, snapshot_path)
            snapshot = RepositorySnapshot.create(
                owner=coordinates.owner,
                repository=coordinates.repository,
                commit_sha=commit_sha,
                root_path=snapshot_path,
                indexed_byte_count=extraction.indexed_byte_count,
                created_at=self._clock(),
            )
        except BaseException:
            shutil.rmtree(snapshot_path, ignore_errors=True)
            raise

        return IngestionResult(
            issue=issue,
            repository=repository,
            recent_commits=recent_commits,
            related_issues=related_issues,
            snapshot=snapshot,
        )


class SnapshotCleaner:
    """Delete expired snapshot directories only beneath one configured root."""

    def __init__(self, snapshot_root: Path) -> None:
        self._snapshot_root = snapshot_root.resolve()

    def cleanup_expired(
        self,
        snapshots: Iterable[RepositorySnapshot],
        *,
        now: datetime | None = None,
    ) -> tuple[Path, ...]:
        timestamp = now or datetime.now(UTC)
        removed: list[Path] = []
        for snapshot in snapshots:
            if snapshot.cleanup_deadline > timestamp:
                continue
            configured_path = snapshot.root_path
            candidate = configured_path.resolve()
            if (
                candidate == self._snapshot_root
                or not candidate.is_relative_to(self._snapshot_root)
                or configured_path.is_symlink()
                or (candidate.exists() and not candidate.is_dir())
            ):
                raise SnapshotScopeError()
            if candidate.exists():
                shutil.rmtree(candidate)
                removed.append(candidate)
        return tuple(removed)
