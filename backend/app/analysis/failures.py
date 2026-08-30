from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.agent import (
    ModelSafetyError,
    ModelSchemaError,
    PermanentModelError,
    TransientModelError,
)
from app.ingestion import (
    ArchiveDownloadLimitError,
    GithubRateLimitError,
    GithubTimeoutError,
    GithubUpstreamError,
    InvalidRepositoryInputError,
    MalformedGithubResponseError,
    RepositoryNotFoundError,
    RepositoryTooLargeError,
    SnapshotScopeError,
    SourceLimitError,
    UnsupportedRepositoryLanguageError,
    UnsafeArchiveError,
)
from app.investigation import InvestigationError


@dataclass(frozen=True, slots=True)
class PublicFailure:
    code: str
    message: str
    http_status: int


_INTERNAL_FAILURE = PublicFailure(
    "internal_error", "Analysis failed safely.", 500
)
_CANONICAL_FAILURES = frozenset(
    {
        PublicFailure("invalid_request", "Repository input is invalid.", 422),
        PublicFailure("not_found", "Public repository or issue was not found.", 404),
        PublicFailure(
            "upstream_unavailable", "GitHub is temporarily unavailable.", 503
        ),
        PublicFailure(
            "repository_rejected",
            "Repository failed a static-analysis safety limit.",
            422,
        ),
        PublicFailure(
            "model_unavailable", "Model provider is temporarily unavailable.", 503
        ),
        PublicFailure("analysis_failed", "Analysis failed safely.", 500),
        PublicFailure("analysis_failed", "Static analysis failed safely.", 500),
        _INTERNAL_FAILURE,
    }
)


_UPSTREAM_FAILURES = (
    GithubRateLimitError,
    GithubTimeoutError,
    GithubUpstreamError,
    MalformedGithubResponseError,
)
_REPOSITORY_REJECTIONS = (
    ArchiveDownloadLimitError,
    RepositoryTooLargeError,
    SourceLimitError,
    UnsupportedRepositoryLanguageError,
    UnsafeArchiveError,
)
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "github_token",
    "model_key",
    "api_key",
    "password",
    "secret",
    "prompt",
)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:sk|gh[oprsu])-[A-Za-z0-9_-]{4,}\b|\bgh[pors]_[A-Za-z0-9]{8,}\b"
)
_CREDENTIAL_URL_PATTERN = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.I)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)


def map_public_failure(error: Exception) -> PublicFailure:
    if isinstance(error, InvalidRepositoryInputError):
        return PublicFailure("invalid_request", "Repository input is invalid.", 422)
    if isinstance(error, RepositoryNotFoundError):
        return PublicFailure("not_found", "Public repository or issue was not found.", 404)
    if isinstance(error, _UPSTREAM_FAILURES):
        return PublicFailure(
            "upstream_unavailable", "GitHub is temporarily unavailable.", 503
        )
    if isinstance(error, _REPOSITORY_REJECTIONS):
        return PublicFailure(
            "repository_rejected", "Repository failed a static-analysis safety limit.", 422
        )
    if isinstance(error, TransientModelError):
        return PublicFailure(
            "model_unavailable", "Model provider is temporarily unavailable.", 503
        )
    if isinstance(error, (ModelSchemaError, ModelSafetyError, PermanentModelError)):
        return PublicFailure("analysis_failed", "Analysis failed safely.", 500)
    if isinstance(error, (InvestigationError, SnapshotScopeError)):
        return PublicFailure("analysis_failed", "Static analysis failed safely.", 500)
    return _INTERNAL_FAILURE


def canonical_public_failure(failure: PublicFailure) -> PublicFailure:
    """Reject caller-controlled codes/messages at the persistence boundary."""
    return failure if failure in _CANONICAL_FAILURES else _INTERNAL_FAILURE


def redact_public_data(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            folded = key.casefold().replace("-", "_")
            if _sensitive_key(folded):
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = redact_public_data(item)
        return cleaned
    if isinstance(value, (tuple, list)):
        return [redact_public_data(item) for item in value]
    if isinstance(value, str):
        redacted = _CREDENTIAL_URL_PATTERN.sub(r"\1[REDACTED]@", value)
        redacted = _AUTHORIZATION_PATTERN.sub("[REDACTED]", redacted)
        return _SECRET_PATTERN.sub("[REDACTED]", redacted)
    if value is None or type(value) in {bool, int, float}:
        return value
    return "[REDACTED]"


def _sensitive_key(folded: str) -> bool:
    return (
        folded in {"message", "messages", "raw_messages"}
        or folded.endswith("_token")
        or any(part in folded for part in _SENSITIVE_KEY_PARTS)
    )
