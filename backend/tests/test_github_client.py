from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.ingestion import (
    ArchiveDownloadLimitError,
    GithubClient,
    GithubRateLimitError,
    GithubTimeoutError,
    GithubUpstreamError,
    MalformedGithubResponseError,
    RepositoryCoordinates,
    RepositoryNotFoundError,
    RepositoryTooLargeError,
)

COORDINATES = RepositoryCoordinates(owner="openai", repository="codex")
SHA = "a" * 40


class _CountingAsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunk_sizes: list[int]) -> None:
        self.chunk_sizes = chunk_sizes
        self.iteration_count = 0

    async def __aiter__(self):
        for size in self.chunk_sizes:
            self.iteration_count += 1
            yield b"z" * size

    async def aclose(self) -> None:
        return None


def _issue_payload(number: int = 7) -> dict[str, object]:
    return {
        "number": number,
        "title": "Parser fails on aliases",
        "body": "Reproduction details",
        "state": "open",
        "html_url": f"https://github.com/openai/codex/issues/{number}",
    }


def _repository_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "codex",
        "owner": {"login": "openai"},
        "default_branch": "main",
        "size": 1_024,
        "private": False,
        "language": "Python",
        "html_url": "https://github.com/openai/codex",
    }
    payload.update(overrides)
    return payload


def _commit_payload(sha: str = SHA) -> dict[str, object]:
    return {
        "sha": sha,
        "html_url": f"https://github.com/openai/codex/commit/{sha}",
        "commit": {
            "message": "Fix parser",
            "committer": {"date": "2026-08-28T12:30:00Z"},
        },
    }


@pytest.mark.anyio
async def test_client_fetches_read_only_metadata_and_sha_then_downloads_codeload_zip() -> None:
    """Breaks if ingestion leaves the API/codeload allowlist or uses a mutable archive ref."""
    observed: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.method, str(request.url), dict(request.headers)))
        if request.url.path.endswith("/issues/7"):
            return httpx.Response(200, json=_issue_payload())
        if request.url.path == "/repos/openai/codex":
            return httpx.Response(200, json=_repository_payload())
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json=_commit_payload())
        if request.url.path == "/repos/openai/codex/commits":
            return httpx.Response(200, json=[_commit_payload()])
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"total_count": 1, "items": [_issue_payload(8)]})
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=b"PK\x03\x04archive")
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GithubClient(http, token="test-token")
        issue = await client.fetch_issue(COORDINATES, 7)
        repository = await client.fetch_repository(COORDINATES)
        head_sha = await client.fetch_default_branch_head(COORDINATES, "main")
        commits = await client.fetch_recent_commits(COORDINATES, sha=head_sha, limit=5)
        related = await client.fetch_related_issues(COORDINATES, "parser aliases", limit=5)
        archive = await client.download_archive(COORDINATES, head_sha)

    assert issue.number == 7
    assert repository.default_branch == "main"
    assert repository.size_kb == 1_024
    assert repository.language == "Python"
    assert head_sha == SHA
    assert commits[0].committed_at == datetime(2026, 8, 28, 12, 30, tzinfo=UTC)
    assert related[0].number == 8
    assert archive == b"PK\x03\x04archive"
    assert all(method == "GET" for method, _, _ in observed)
    assert {httpx.URL(url).host for _, url, _ in observed} == {
        "api.github.com",
        "codeload.github.com",
    }
    assert observed[-1][1] == f"https://codeload.github.com/openai/codex/zip/{SHA}"
    api_headers = [headers for _, url, headers in observed if httpx.URL(url).host == "api.github.com"]
    codeload_headers = [
        headers for _, url, headers in observed if httpx.URL(url).host == "codeload.github.com"
    ]
    assert all(headers["authorization"] == "Bearer test-token" for headers in api_headers)
    assert all("authorization" not in headers for headers in codeload_headers)


@pytest.mark.anyio
async def test_recent_commits_query_is_anchored_to_resolved_sha() -> None:
    """Breaks if history can drift to a newer default-branch head during analysis."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/openai/codex/commits"
        assert request.url.params["sha"] == SHA
        assert request.url.params["per_page"] == "5"
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        commits = await GithubClient(http).fetch_recent_commits(
            COORDINATES, sha=SHA, limit=5
        )

    assert commits == ()


@pytest.mark.anyio
async def test_recent_commits_optional_path_stays_on_allowlisted_query_boundary() -> None:
    """Breaks if a tool path becomes a URL or is omitted from the read-only GitHub query."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/openai/codex/commits"
        assert dict(request.url.params) == {
            "sha": SHA,
            "per_page": "10",
            "path": "src/parser.py",
        }
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        commits = await GithubClient(http).fetch_recent_commits(
            COORDINATES, sha=SHA, path="src/parser.py"
        )

    assert commits == ()


@pytest.mark.anyio
async def test_client_reads_optional_token_from_settings_without_exposing_it() -> None:
    """Breaks if configured credentials are omitted from requests or revealed by settings."""
    token = "github-secret-value"
    settings = Settings(github_token=token)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {token}"
        return httpx.Response(200, json=_repository_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        repository = await GithubClient.from_settings(http, settings).fetch_repository(
            COORDINATES
        )

    assert repository.repository == "codex"
    assert token not in repr(settings)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "headers", "expected_error"),
    [
        (404, {}, RepositoryNotFoundError),
        (403, {"X-RateLimit-Remaining": "0"}, GithubRateLimitError),
        (429, {}, GithubRateLimitError),
        (500, {}, GithubUpstreamError),
    ],
)
async def test_client_maps_http_failures_to_safe_typed_errors(
    status_code: int,
    headers: dict[str, str],
    expected_error: type[Exception],
) -> None:
    """Breaks if callers receive raw HTTP failures or secret-bearing upstream bodies."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers=headers,
            json={"message": "upstream leaked secret"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(expected_error) as caught:
            await GithubClient(http).fetch_repository(COORDINATES)

    assert "upstream leaked secret" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("headers", "payload", "expected_error"),
    [
        (
            {"Retry-After": "60"},
            {"message": "Please wait before retrying secret request"},
            GithubRateLimitError,
        ),
        (
            {},
            {
                "message": "API rate limit exceeded for 192.0.2.1.",
                "documentation_url": "https://docs.github.com/rest/using-the-rest-api/rate-limits-for-the-rest-api",
                "status": "403",
            },
            GithubRateLimitError,
        ),
        (
            {},
            {
                "message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again.",
                "documentation_url": "https://docs.github.com/rest/using-the-rest-api/rate-limits-for-the-rest-api",
                "status": "403",
            },
            GithubRateLimitError,
        ),
        (
            {},
            {"message": "Repository documentation mentions rate limits: secret"},
            GithubUpstreamError,
        ),
        ({}, {"message": ["API rate limit exceeded"]}, GithubUpstreamError),
        ({}, ["API rate limit exceeded"], GithubUpstreamError),
    ],
)
async def test_client_classifies_complete_secondary_rate_limit_response_shapes(
    headers: dict[str, str],
    payload: object,
    expected_error: type[Exception],
) -> None:
    """Breaks if documented rate-limit signals are missed or arbitrary bodies match."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers=headers, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(expected_error) as caught:
            await GithubClient(http).fetch_repository(COORDINATES)

    assert "secret" not in str(caught.value)
    assert "192.0.2.1" not in str(caught.value)
    assert "Please wait a few minutes" not in str(caught.value)


@pytest.mark.anyio
async def test_client_maps_timeout_to_safe_typed_error() -> None:
    """Breaks if transport timeouts escape as unhandled httpx exceptions."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request included token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GithubTimeoutError) as caught:
            await GithubClient(http).fetch_repository(COORDINATES)

    assert "token" not in str(caught.value)


@pytest.mark.anyio
async def test_issue_endpoint_rejects_pull_request_payload_as_not_found() -> None:
    """Breaks if a pull request number can satisfy the bug-issue input contract."""
    payload = {
        **_issue_payload(),
        "pull_request": {
            "url": "https://api.github.com/repos/openai/codex/pulls/7",
            "html_url": "https://github.com/openai/codex/pull/7",
            "diff_url": "https://github.com/openai/codex/pull/7.diff",
            "patch_url": "https://github.com/openai/codex/pull/7.patch",
            "merged_at": None,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RepositoryNotFoundError):
            await GithubClient(http).fetch_issue(COORDINATES, 7)


@pytest.mark.anyio
async def test_client_never_follows_redirects_outside_the_host_allowlist() -> None:
    """Breaks if injected client redirect settings can send requests to another host."""
    observed_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_hosts.append(request.url.host)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"Location": "https://example.com/leak"})
        return httpx.Response(200, json=_repository_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        with pytest.raises(GithubUpstreamError):
            await GithubClient(http, token="must-not-leak").fetch_repository(COORDINATES)

    assert observed_hosts == ["api.github.com"]


@pytest.mark.anyio
async def test_archive_download_rejects_oversized_content_length_before_iteration() -> None:
    """Breaks if an announced oversized codeload body is read into memory."""
    stream = _CountingAsyncStream([1])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "50000001"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ArchiveDownloadLimitError):
            await GithubClient(http).download_archive(COORDINATES, SHA)

    assert stream.iteration_count == 0


@pytest.mark.anyio
async def test_archive_download_stops_stream_at_first_byte_above_50_000_000() -> None:
    """Breaks if an unannounced oversized codeload stream is fully buffered."""
    stream = _CountingAsyncStream([30_000_000, 20_000_000, 1, 1_000_000])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ArchiveDownloadLimitError):
            await GithubClient(http).download_archive(COORDINATES, SHA)

    assert stream.iteration_count == 3


@pytest.mark.anyio
async def test_client_treats_private_and_oversized_repositories_as_domain_failures() -> None:
    """Breaks if private source or metadata above 50,000 KB reaches download."""
    payloads = iter(
        [
            _repository_payload(private=True),
            _repository_payload(size=50_001),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(payloads))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GithubClient(http)
        with pytest.raises(RepositoryNotFoundError):
            await client.fetch_repository(COORDINATES)
        with pytest.raises(RepositoryTooLargeError):
            await client.fetch_repository(COORDINATES)


@pytest.mark.anyio
async def test_client_accepts_repository_metadata_at_exact_50_000_kb_limit() -> None:
    """Breaks if the documented metadata ceiling rejects its exact boundary."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_repository_payload(size=50_000))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        repository = await GithubClient(http).fetch_repository(COORDINATES)

    assert repository.size_kb == 50_000


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"default_branch": "main"},
        _repository_payload(size="large"),
        _repository_payload(owner={"login": 42}),
    ],
)
async def test_client_rejects_malformed_repository_payloads(payload: dict[str, object]) -> None:
    """Breaks if malformed GitHub metadata is trusted as repository identity or limits."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MalformedGithubResponseError):
            await GithubClient(http).fetch_repository(COORDINATES)


@pytest.mark.anyio
async def test_client_rejects_non_sha_default_branch_resolution() -> None:
    """Breaks if a mutable or malformed ref can become the archive identity."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_commit_payload("main"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MalformedGithubResponseError):
            await GithubClient(http).fetch_default_branch_head(COORDINATES, "main")
