from __future__ import annotations

from pathlib import Path


def _workspace_roots() -> set[Path]:
    roots = {Path.cwd().resolve()}
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for candidate in (start, *start.parents):
            if (
                (candidate / ".git").exists()
                or (
                    (candidate / "pyproject.toml").exists()
                    and (candidate / "backend").exists()
                    and (candidate / "frontend").exists()
                )
            ):
                roots.add(candidate.resolve())
    return roots


def validate_snapshot_root(root: object) -> Path:
    """Return a resolved dedicated snapshot root or reject broad deletion scopes."""
    if not isinstance(root, (str, Path)):
        raise ValueError("snapshot_root must be a filesystem path")
    raw = str(root).strip()
    if raw in {"", ".", "./", ".\\"}:
        raise ValueError("snapshot_root must be a dedicated snapshot directory")

    candidate = Path(root).expanduser()
    if candidate.exists() and candidate.is_symlink():
        raise ValueError("snapshot_root must not be a symlink")

    resolved = candidate.resolve(strict=False)
    anchor = Path(resolved.anchor).resolve(strict=False)
    if resolved == anchor or resolved.parent == resolved:
        raise ValueError("snapshot_root must not be a filesystem root")
    if resolved.parent == anchor:
        raise ValueError("snapshot_root must not be directly under a filesystem root")
    if resolved == Path.home().resolve(strict=False):
        raise ValueError("snapshot_root must not be the user home directory")
    if resolved in _workspace_roots():
        raise ValueError("snapshot_root must not be the repository workspace root")
    if "snapshot" not in resolved.name.casefold():
        raise ValueError("snapshot_root must be a dedicated snapshot directory")
    return resolved
