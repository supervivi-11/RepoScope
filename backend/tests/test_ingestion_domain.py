from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.ingestion import (
    InvalidRepositoryInputError,
    RepositoryCoordinates,
    RepositorySnapshot,
    validate_issue_number,
)


@pytest.mark.parametrize(
    ("raw_url", "owner", "repository"),
    [
        ("https://github.com/openai/codex", "openai", "codex"),
        ("https://github.com/openai/codex/", "openai", "codex"),
        ("https://github.com/openai/codex.git", "openai", "codex"),
        ("https://github.com/openai/codex.git/", "openai", "codex"),
    ],
)
def test_repository_coordinates_normalize_supported_github_urls(
    raw_url: str, owner: str, repository: str
) -> None:
    """Breaks if supported URL spellings do not resolve to one coordinate pair."""
    coordinates = RepositoryCoordinates.parse(raw_url)

    assert coordinates.owner == owner
    assert coordinates.repository == repository
    assert coordinates.canonical_url == "https://github.com/openai/codex"


@pytest.mark.parametrize(
    "raw_url",
    [
        "http://github.com/openai/codex",
        "ssh://github.com/openai/codex",
        "https://gitlab.com/openai/codex",
        "https://api.github.com/openai/codex",
        "https://user:secret@github.com/openai/codex",
        "https://github.com:443/openai/codex",
        "https://github.com/openai/codex?tab=readme",
        "https://github.com/openai/codex#readme",
        "https://github.com/openai/codex/issues",
        "https://github.com/openai",
        "https://github.com/openai//codex",
        "https://github.com/../codex",
        "https://github.com/openai/.",
        "https://github.com/openai/%2e%2e",
        "https://github.com/openai/co%2fdex",
        "https://github.com/open ai/codex",
    ],
)
def test_repository_coordinates_reject_everything_outside_the_public_contract(
    raw_url: str,
) -> None:
    """Breaks if hostile or ambiguous repository URLs reach GitHub ingestion."""
    with pytest.raises(InvalidRepositoryInputError):
        RepositoryCoordinates.parse(raw_url)


@pytest.mark.parametrize("issue_number", [1, 42, 2_147_483_647])
def test_issue_number_accepts_positive_integers(issue_number: int) -> None:
    """Breaks if a valid GitHub issue identifier is rejected or changed."""
    assert validate_issue_number(issue_number) == issue_number


@pytest.mark.parametrize("issue_number", [0, -1, True, 1.5, "1", None])
def test_issue_number_rejects_non_positive_or_non_integer_values(issue_number: object) -> None:
    """Breaks if invalid issue identifiers can alter the read-only endpoint path."""
    with pytest.raises(InvalidRepositoryInputError):
        validate_issue_number(issue_number)


def test_repository_snapshot_is_immutable_and_defaults_cleanup_to_24_hours() -> None:
    """Breaks if an immutable source identity can drift after indexing starts."""
    created_at = datetime(2026, 8, 29, 4, 0, tzinfo=UTC)
    snapshot = RepositorySnapshot.create(
        owner="openai",
        repository="codex",
        commit_sha="a" * 40,
        root_path=Path("snapshot"),
        indexed_byte_count=123,
        created_at=created_at,
    )

    assert snapshot.cleanup_deadline == created_at + timedelta(hours=24)
    with pytest.raises(FrozenInstanceError):
        snapshot.commit_sha = "b" * 40  # type: ignore[misc]
