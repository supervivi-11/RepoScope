from __future__ import annotations

import json
import re
from dataclasses import dataclass
from math import isfinite
from typing import Any

from app.agent import (
    AnalysisReport,
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
ATTEMPTS_EXHAUSTED_FAILURE = PublicFailure(
    "attempts_exhausted",
    "Analysis retry budget was exhausted.",
    500,
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
        ATTEMPTS_EXHAUSTED_FAILURE,
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
)
_PAYLOAD_KEY_TOKENS = frozenset(
    {"content", "contents", "message", "messages", "prompt", "prompts"}
)
_SAFE_PAYLOAD_METADATA_SUFFIXES = frozenset({"count", "type"})
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:sk|gh[oprsu])-[A-Za-z0-9_-]+\b"
    r"|\bgh[pors]_[A-Za-z0-9]+\b|\bgithub_pat_[A-Za-z0-9_]+\b"
)
_CREDENTIAL_URL_PATTERN = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.I)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)authorization[ \t]*[\"']?[ \t]*[:=,][ \t]*[\"']?[ \t]*"
    r"(?:bearer|basic)[ \t]+[^\"',; \t)]+"
)
_STANDALONE_AUTH_PATTERN = re.compile(
    r"(?i)\b(?:bearer|basic)[ \t]+[^\"',; \t)]+"
)
_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<prefix>[\"']?(?P<key>[A-Za-z0-9_-]+)[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_UNQUOTED_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<prefix>[\"']?(?P<key>[A-Za-z0-9_-]+)[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<value>[^\"',; \t)]+)"
)
_LINE_SEPARATOR_PATTERN = re.compile(r"(\r\n|\n|\r)")
_PRIVATE_KEY_BEGIN_PATTERN = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----", re.I)
_PRIVATE_KEY_END_PATTERN = re.compile(r"-----END [^-]*PRIVATE KEY-----", re.I)


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
            if _sensitive_key(key):
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = redact_public_data(item)
        return cleaned
    if isinstance(value, (tuple, list)):
        return [redact_public_data(item) for item in value]
    if isinstance(value, str):
        return redact_public_text(value)
    if value is None or type(value) in {bool, int, float}:
        return value
    return "[REDACTED]"


def redact_public_text(value: str) -> str:
    """Redact credentials without changing any original line delimiter."""
    parts = _LINE_SEPARATOR_PATTERN.split(value)
    private_key = False
    for index in range(0, len(parts), 2):
        line = parts[index]
        if _PRIVATE_KEY_BEGIN_PATTERN.search(line):
            private_key = True
            parts[index] = "[REDACTED PRIVATE KEY]"
            continue
        if private_key:
            parts[index] = "[REDACTED PRIVATE KEY]"
            if _PRIVATE_KEY_END_PATTERN.search(line):
                private_key = False
            continue
        redacted = _CREDENTIAL_URL_PATTERN.sub(r"\1[REDACTED]@", line)
        redacted = _AUTHORIZATION_PATTERN.sub("[REDACTED]", redacted)
        redacted = _STANDALONE_AUTH_PATTERN.sub("[REDACTED]", redacted)
        redacted = _QUOTED_ASSIGNMENT_PATTERN.sub(
            _redact_quoted_assignment,
            redacted,
        )
        redacted = _UNQUOTED_ASSIGNMENT_PATTERN.sub(
            _redact_unquoted_assignment,
            redacted,
        )
        parts[index] = _SECRET_PATTERN.sub("[REDACTED]", redacted)
    return "".join(parts)


def _sensitive_text_key(key: str) -> bool:
    folded = re.sub(r"[-_]+", "_", key.casefold()).strip("_")
    return folded in {"token", "password", "secret"} or folded.endswith(
        (
            "_token",
            "_password",
            "_secret",
            "_client_secret",
            "_api_key",
            "_access_key",
            "_access_key_id",
            "_private_key",
        )
    )


def _redact_quoted_assignment(match: re.Match[str]) -> str:
    if not _sensitive_text_key(match.group("key")):
        return match.group(0)
    quote = match.group("quote")
    return f'{match.group("prefix")}{quote}[REDACTED]{quote}'


def _redact_unquoted_assignment(match: re.Match[str]) -> str:
    if not _sensitive_text_key(match.group("key")):
        return match.group(0)
    return f'{match.group("prefix")}[REDACTED]'


def sanitize_public_report(report: AnalysisReport) -> AnalysisReport:
    cleaned = redact_public_data(report.model_dump(mode="json"))
    return AnalysisReport.model_validate(cleaned)


def _sensitive_key(key: str) -> bool:
    separated = _CAMEL_CASE_BOUNDARY.sub("_", key)
    tokens = tuple(_KEY_TOKEN_PATTERN.findall(separated.casefold()))
    if not tokens:
        return False
    folded = "_".join(tokens)
    if (
        folded.endswith("_token")
        or any(part in folded for part in _SENSITIVE_KEY_PARTS)
    ):
        return True
    payload_tokens = _PAYLOAD_KEY_TOKENS.intersection(tokens)
    if not payload_tokens:
        return False
    return tokens[-1] not in _SAFE_PAYLOAD_METADATA_SUFFIXES


def sanitize_public_mapping(
    value: dict[str, Any], *, counters: bool = False
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("public data must be a JSON object")
    _validate_public_input(value, depth=0, entry_count=[0])
    cleaned = redact_public_data(value)
    if not isinstance(cleaned, dict):
        raise ValueError("public data must be a JSON object")
    entry_count = [0]
    _validate_public_json(cleaned, depth=0, entry_count=entry_count)
    if counters:
        for item in cleaned.values():
            if item == "[REDACTED]":
                continue
            if type(item) is not int or item < 0:
                raise ValueError("public counters must be nonnegative integers")
    encoded = json.dumps(cleaned, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16_384:
        raise ValueError("public data exceeds the storage limit")
    return cleaned


def safe_public_mapping(
    value: Any, *, counters: bool = False
) -> dict[str, Any]:
    try:
        return sanitize_public_mapping(value, counters=counters)
    except (TypeError, ValueError):
        return {}


def safe_public_error(
    code: str | None, message: str | None
) -> tuple[str | None, str | None]:
    if code is None and message is None:
        return None, None
    if any(item.code == code and item.message == message for item in _CANONICAL_FAILURES):
        return code, message
    return _INTERNAL_FAILURE.code, _INTERNAL_FAILURE.message


def _validate_public_json(
    value: Any, *, depth: int, entry_count: list[int]
) -> None:
    if depth > 8:
        raise ValueError("public data nesting is too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            entry_count[0] += 1
            if entry_count[0] > 256 or not isinstance(key, str) or len(key) > 100:
                raise ValueError("public data contains invalid keys")
            _validate_public_json(
                item, depth=depth + 1, entry_count=entry_count
            )
        return
    if isinstance(value, list):
        entry_count[0] += len(value)
        if entry_count[0] > 256:
            raise ValueError("public data contains too many values")
        for item in value:
            _validate_public_json(
                item, depth=depth + 1, entry_count=entry_count
            )
        return
    if isinstance(value, str):
        if len(value) > 4_000:
            raise ValueError("public data text is too long")
        return
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float and isfinite(value):
        return
    raise ValueError("public data must contain finite JSON values")


def _validate_public_input(
    value: Any, *, depth: int, entry_count: list[int]
) -> None:
    if depth > 8:
        raise ValueError("public data nesting is too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            entry_count[0] += 1
            if entry_count[0] > 256 or not isinstance(key, str) or len(key) > 100:
                raise ValueError("public data contains invalid keys")
            if _sensitive_key(key):
                continue
            _validate_public_input(
                item, depth=depth + 1, entry_count=entry_count
            )
        return
    if isinstance(value, (tuple, list)):
        entry_count[0] += len(value)
        if entry_count[0] > 256:
            raise ValueError("public data contains too many values")
        for item in value:
            _validate_public_input(
                item, depth=depth + 1, entry_count=entry_count
            )
        return
    if isinstance(value, str):
        if len(value) > 4_000:
            raise ValueError("public data text is too long")
        return
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float and isfinite(value):
        return
    raise ValueError("public data must contain finite JSON values")
