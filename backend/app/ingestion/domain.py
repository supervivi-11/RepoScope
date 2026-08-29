from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import InvalidRepositoryInputError

_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")


@dataclass(frozen=True, slots=True)
class RepositoryCoordinates:
    owner: str
    repository: str

    @classmethod
    def parse(cls, raw_url: str) -> RepositoryCoordinates:
        if not isinstance(raw_url, str):
            raise InvalidRepositoryInputError()
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.netloc != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidRepositoryInputError()

        path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
        raw_segments = path.split("/")
        if len(raw_segments) != 3 or raw_segments[0] != "":
            raise InvalidRepositoryInputError()
        owner, repository = raw_segments[1:]
        if repository.endswith(".git"):
            repository = repository[:-4]
        decoded = (unquote(owner), unquote(repository))
        if decoded != (owner, repository):
            raise InvalidRepositoryInputError()
        if (
            owner in {".", ".."}
            or repository in {".", ".."}
            or not _SEGMENT_PATTERN.fullmatch(owner)
            or not _SEGMENT_PATTERN.fullmatch(repository)
        ):
            raise InvalidRepositoryInputError()
        return cls(owner=owner, repository=repository)

    @property
    def canonical_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}"


def validate_issue_number(issue_number: object) -> int:
    if type(issue_number) is not int or issue_number <= 0:
        raise InvalidRepositoryInputError()
    return issue_number


@dataclass(frozen=True, slots=True)
class GithubIssue:
    number: int
    title: str
    body: str | None
    state: str
    html_url: str


@dataclass(frozen=True, slots=True)
class RepositoryMetadata:
    owner: str
    repository: str
    default_branch: str
    size_kb: int
    html_url: str


@dataclass(frozen=True, slots=True)
class CommitMetadata:
    sha: str
    message: str
    html_url: str
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    owner: str
    repository: str
    commit_sha: str
    root_path: Path
    indexed_byte_count: int
    created_at: datetime
    cleanup_deadline: datetime

    @classmethod
    def create(
        cls,
        *,
        owner: str,
        repository: str,
        commit_sha: str,
        root_path: Path,
        indexed_byte_count: int,
        created_at: datetime | None = None,
        cleanup_after: timedelta = timedelta(hours=24),
    ) -> RepositorySnapshot:
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise InvalidRepositoryInputError("Snapshot creation time must be timezone-aware.")
        if not _SHA_PATTERN.fullmatch(commit_sha):
            raise InvalidRepositoryInputError("The resolved commit SHA is invalid.")
        if indexed_byte_count < 0 or cleanup_after <= timedelta(0):
            raise InvalidRepositoryInputError("Snapshot metadata is invalid.")
        return cls(
            owner=owner,
            repository=repository,
            commit_sha=commit_sha.lower(),
            root_path=root_path,
            indexed_byte_count=indexed_byte_count,
            created_at=timestamp,
            cleanup_deadline=timestamp + cleanup_after,
        )
