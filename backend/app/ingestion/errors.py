class IngestionError(Exception):
    """Base class for errors safe to expose at the ingestion boundary."""

    safe_message = "Repository ingestion failed."

    def __init__(self, safe_message: str | None = None) -> None:
        super().__init__(safe_message or self.safe_message)


class InvalidRepositoryInputError(IngestionError, ValueError):
    safe_message = "The repository URL or issue number is invalid."


class RepositoryNotFoundError(IngestionError):
    safe_message = "The public repository or issue was not found."


class GithubRateLimitError(IngestionError):
    safe_message = "GitHub's read-only API rate limit was reached."


class GithubTimeoutError(IngestionError):
    safe_message = "GitHub did not respond before the request timed out."


class GithubUpstreamError(IngestionError):
    safe_message = "GitHub could not complete the read-only request."


class MalformedGithubResponseError(IngestionError):
    safe_message = "GitHub returned an invalid response."


class RepositoryTooLargeError(IngestionError):
    safe_message = "The repository is larger than the 50 MB limit."


class ArchiveDownloadLimitError(IngestionError):
    safe_message = "The repository archive download exceeds the 50 MB limit."


class UnsupportedRepositoryLanguageError(IngestionError):
    safe_message = "The repository does not contain retained Python source."


class UnsafeArchiveError(IngestionError):
    safe_message = "The repository archive contains an unsafe entry."


class SourceLimitError(IngestionError):
    safe_message = "The retained repository source exceeds a safety limit."


class SnapshotScopeError(IngestionError):
    safe_message = "The snapshot path is outside the configured snapshot root."
