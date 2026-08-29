from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from .domain import (
    CommitMetadata,
    GithubIssue,
    RepositoryCoordinates,
    RepositoryMetadata,
    validate_issue_number,
)
from .errors import (
    ArchiveDownloadLimitError,
    GithubRateLimitError,
    GithubTimeoutError,
    GithubUpstreamError,
    MalformedGithubResponseError,
    RepositoryNotFoundError,
    RepositoryTooLargeError,
)

if TYPE_CHECKING:
    from app.config import Settings

_API_ORIGIN = "https://api.github.com"
_CODELOAD_ORIGIN = "https://codeload.github.com"
_ALLOWED_HOSTS = {"api.github.com", "codeload.github.com"}
_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")
_MAX_REPOSITORY_KB = 50_000
_MAX_ARCHIVE_RESPONSE_BYTES = 50_000_000
_DEFAULT_TIMEOUT = httpx.Timeout(15.0)


class GithubClient:
    """Read-only GitHub HTTP boundary limited to API and codeload hosts."""

    def __init__(self, http_client: httpx.AsyncClient, token: str | None = None) -> None:
        self._http = http_client
        self._token = token

    @classmethod
    def from_settings(
        cls, http_client: httpx.AsyncClient, settings: Settings
    ) -> GithubClient:
        token = (
            settings.github_token.get_secret_value()
            if settings.github_token is not None
            else None
        )
        return cls(http_client, token=token)

    async def fetch_issue(
        self, coordinates: RepositoryCoordinates, issue_number: int
    ) -> GithubIssue:
        issue_number = validate_issue_number(issue_number)
        payload = await self._request_json(
            f"{_API_ORIGIN}/repos/{coordinates.owner}/{coordinates.repository}"
            f"/issues/{issue_number}"
        )
        return self._parse_issue(payload)

    async def fetch_repository(
        self, coordinates: RepositoryCoordinates
    ) -> RepositoryMetadata:
        payload = await self._request_json(
            f"{_API_ORIGIN}/repos/{coordinates.owner}/{coordinates.repository}"
        )
        private = payload.get("private")
        if type(private) is not bool:
            raise MalformedGithubResponseError()
        if private:
            raise RepositoryNotFoundError()

        owner_payload = payload.get("owner")
        size_kb = payload.get("size")
        language = payload.get("language")
        if (
            not isinstance(owner_payload, Mapping)
            or not isinstance(owner_payload.get("login"), str)
            or not isinstance(payload.get("name"), str)
            or not isinstance(payload.get("default_branch"), str)
            or not payload["default_branch"]
            or type(size_kb) is not int
            or size_kb < 0
            or not isinstance(payload.get("html_url"), str)
            or (language is not None and not isinstance(language, str))
        ):
            raise MalformedGithubResponseError()
        if size_kb > _MAX_REPOSITORY_KB:
            raise RepositoryTooLargeError()
        return RepositoryMetadata(
            owner=owner_payload["login"],
            repository=payload["name"],
            default_branch=payload["default_branch"],
            size_kb=size_kb,
            html_url=payload["html_url"],
            language=language,
        )

    async def fetch_default_branch_head(
        self, coordinates: RepositoryCoordinates, default_branch: str
    ) -> str:
        encoded_branch = quote(default_branch, safe="")
        payload = await self._request_json(
            f"{_API_ORIGIN}/repos/{coordinates.owner}/{coordinates.repository}"
            f"/commits/{encoded_branch}"
        )
        sha = payload.get("sha")
        if not isinstance(sha, str) or not _SHA_PATTERN.fullmatch(sha):
            raise MalformedGithubResponseError()
        return sha.lower()

    async def fetch_recent_commits(
        self,
        coordinates: RepositoryCoordinates,
        *,
        sha: str,
        path: str | None = None,
        limit: int = 10,
    ) -> tuple[CommitMetadata, ...]:
        if not _SHA_PATTERN.fullmatch(sha):
            raise MalformedGithubResponseError()
        params = {"sha": sha.lower(), "per_page": str(limit)}
        if path is not None:
            params["path"] = path
        payload = await self._request_json(
            f"{_API_ORIGIN}/repos/{coordinates.owner}/{coordinates.repository}/commits",
            params=params,
            expect_mapping=False,
        )
        if not isinstance(payload, list):
            raise MalformedGithubResponseError()
        return tuple(self._parse_commit(item) for item in payload)

    async def fetch_related_issues(
        self,
        coordinates: RepositoryCoordinates,
        query: str,
        *,
        limit: int = 10,
    ) -> tuple[GithubIssue, ...]:
        payload = await self._request_json(
            f"{_API_ORIGIN}/search/issues",
            params={
                "q": f"repo:{coordinates.owner}/{coordinates.repository} is:issue {query}",
                "per_page": str(limit),
            },
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise MalformedGithubResponseError()
        return tuple(self._parse_issue(item) for item in items)

    async def download_archive(
        self, coordinates: RepositoryCoordinates, commit_sha: str
    ) -> bytes:
        if not _SHA_PATTERN.fullmatch(commit_sha):
            raise MalformedGithubResponseError()
        url = (
            f"{_CODELOAD_ORIGIN}/{coordinates.owner}/{coordinates.repository}"
            f"/zip/{commit_sha.lower()}"
        )
        headers = {
            "Accept": "application/zip",
            "User-Agent": "RepoScope/0.1",
        }
        try:
            async with self._http.stream(
                "GET",
                url,
                headers=headers,
                timeout=_DEFAULT_TIMEOUT,
                follow_redirects=False,
            ) as response:
                if response.status_code == 404:
                    raise RepositoryNotFoundError()
                if response.status_code == 429 or (
                    response.status_code == 403
                    and (
                        response.headers.get("X-RateLimit-Remaining") == "0"
                        or "Retry-After" in response.headers
                    )
                ):
                    raise GithubRateLimitError()
                if not response.is_success:
                    raise GithubUpstreamError()

                announced_size = response.headers.get("Content-Length")
                if announced_size is not None:
                    try:
                        exceeds_limit = int(announced_size) > _MAX_ARCHIVE_RESPONSE_BYTES
                    except ValueError:
                        exceeds_limit = False
                    if exceeds_limit:
                        raise ArchiveDownloadLimitError()

                archive = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(archive) + len(chunk) > _MAX_ARCHIVE_RESPONSE_BYTES:
                        raise ArchiveDownloadLimitError()
                    archive.extend(chunk)
                return bytes(archive)
        except httpx.TimeoutException as exc:
            raise GithubTimeoutError() from exc
        except httpx.RequestError as exc:
            raise GithubUpstreamError() from exc

    async def _request_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        expect_mapping: bool = True,
    ) -> Any:
        response = await self._request(url, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MalformedGithubResponseError() from exc
        if expect_mapping and not isinstance(payload, Mapping):
            raise MalformedGithubResponseError()
        return payload

    async def _request(
        self, url: str, *, params: Mapping[str, str] | None = None
    ) -> httpx.Response:
        parsed = httpx.URL(url)
        if (
            parsed.scheme != "https"
            or parsed.host not in _ALLOWED_HOSTS
            or parsed.port not in (None, 443)
        ):
            raise GithubUpstreamError()
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "RepoScope/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token and parsed.host == "api.github.com":
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.get(
                parsed,
                params=params,
                headers=headers,
                timeout=_DEFAULT_TIMEOUT,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise GithubTimeoutError() from exc
        except httpx.RequestError as exc:
            raise GithubUpstreamError() from exc
        if response.status_code == 404:
            raise RepositoryNotFoundError()
        if self._is_rate_limited(response):
            raise GithubRateLimitError()
        if not response.is_success:
            raise GithubUpstreamError()
        return response

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or "Retry-After" in response.headers
        ):
            return True
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, Mapping):
            return False
        message = payload.get("message")
        if not isinstance(message, str):
            return False
        normalized = message.casefold()
        return normalized.startswith("api rate limit exceeded") or normalized.startswith(
            "you have exceeded a secondary rate limit"
        )

    @staticmethod
    def _parse_issue(payload: object) -> GithubIssue:
        if not isinstance(payload, Mapping):
            raise MalformedGithubResponseError()
        if "pull_request" in payload:
            raise RepositoryNotFoundError()
        number = payload.get("number")
        title = payload.get("title")
        body = payload.get("body")
        state = payload.get("state")
        html_url = payload.get("html_url")
        if (
            type(number) is not int
            or number <= 0
            or not isinstance(title, str)
            or (body is not None and not isinstance(body, str))
            or not isinstance(state, str)
            or not isinstance(html_url, str)
        ):
            raise MalformedGithubResponseError()
        return GithubIssue(
            number=number, title=title, body=body, state=state, html_url=html_url
        )

    @staticmethod
    def _parse_commit(payload: object) -> CommitMetadata:
        if not isinstance(payload, Mapping):
            raise MalformedGithubResponseError()
        commit = payload.get("commit")
        if not isinstance(commit, Mapping):
            raise MalformedGithubResponseError()
        committer = commit.get("committer")
        sha = payload.get("sha")
        message = commit.get("message")
        html_url = payload.get("html_url")
        committed_at = committer.get("date") if isinstance(committer, Mapping) else None
        if (
            not isinstance(sha, str)
            or not _SHA_PATTERN.fullmatch(sha)
            or not isinstance(message, str)
            or not isinstance(html_url, str)
            or not isinstance(committed_at, str)
        ):
            raise MalformedGithubResponseError()
        try:
            timestamp = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MalformedGithubResponseError() from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise MalformedGithubResponseError()
        return CommitMetadata(
            sha=sha.lower(),
            message=message,
            html_url=html_url,
            committed_at=timestamp,
        )
