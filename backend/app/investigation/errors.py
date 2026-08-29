class InvestigationError(ValueError):
    """Safe base error for static repository investigation."""


class UnsafeSnapshotError(InvestigationError):
    """The snapshot no longer satisfies ingestion safety invariants."""


class InvalidSourcePathError(InvestigationError):
    """A caller supplied a non-relative or escaping source path."""


class SourceNotFoundError(InvestigationError):
    """A requested retained source file does not exist in the index."""


class SourceRangeError(InvestigationError):
    """A requested inclusive line range is invalid or too large."""


class InvalidQueryError(InvestigationError):
    """A search query is empty or otherwise outside the bounded contract."""
