from __future__ import annotations

from app.ingestion import GithubClient, RepositoryCoordinates

from .domain import (
    CommitRecord,
    IssueRecord,
    RecentCommitsResult,
    ReferenceResults,
    RelatedIssuesResult,
    RepositoryMap,
    SearchResults,
    SourceExcerpt,
    SymbolResults,
)
from .errors import InvalidQueryError
from .index import PythonRepositoryIndex, _normalize_relative_path


class InvestigationTools:
    """The complete, bounded, read-only tool surface used by the agent graph."""

    def __init__(self, index: PythonRepositoryIndex, github: GithubClient) -> None:
        self._index = index
        self._github = github
        self._coordinates = RepositoryCoordinates(
            owner=index.snapshot.owner,
            repository=index.snapshot.repository,
        )
        self._commit_sha = index.snapshot.commit_sha

    @property
    def index(self) -> PythonRepositoryIndex:
        """The immutable local index used for deterministic citation checks."""
        return self._index

    def get_repository_map(self) -> RepositoryMap:
        return self._index.repository_map

    def search_code(
        self, query: str, path_prefix: str | None = None
    ) -> SearchResults:
        return self._index.search_code(query, path_prefix)

    def read_code(self, path: str, start_line: int, end_line: int) -> SourceExcerpt:
        return self._index.read_code(path, start_line, end_line)

    def find_symbol(self, symbol: str) -> SymbolResults:
        return self._index.find_symbol(symbol)

    def find_references(self, symbol: str) -> ReferenceResults:
        return self._index.find_references(symbol)

    async def get_recent_commits(
        self, path: str | None = None
    ) -> RecentCommitsResult:
        normalized_path = (
            _normalize_relative_path(path) if path is not None else None
        )
        commits = await self._github.fetch_recent_commits(
            self._coordinates,
            sha=self._commit_sha,
            path=normalized_path,
            limit=10,
        )
        records = tuple(
            CommitRecord(
                sha=item.sha,
                message=item.message,
                html_url=item.html_url,
                committed_at=item.committed_at,
            )
            for item in commits
        )
        return RecentCommitsResult(
            commits=tuple(
                sorted(
                    records,
                    key=lambda item: (-item.committed_at.timestamp(), item.sha),
                )
            )
        )

    async def get_related_issues(self, query: str) -> RelatedIssuesResult:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise InvalidQueryError(
                "Related-issue query must contain 1 to 500 characters."
            )
        issues = await self._github.fetch_related_issues(
            self._coordinates, query, limit=10
        )
        records = tuple(
            IssueRecord(
                number=item.number,
                title=item.title,
                body=item.body,
                state=item.state,
                html_url=item.html_url,
            )
            for item in issues
        )
        return RelatedIssuesResult(
            issues=tuple(sorted(records, key=lambda item: item.number))
        )
