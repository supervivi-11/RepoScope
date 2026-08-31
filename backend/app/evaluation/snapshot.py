from __future__ import annotations

import hashlib
import os
from pathlib import Path


MAX_EVALUATION_SNAPSHOT_BYTES = 10_000_000
MAX_EVALUATION_FILE_BYTES = 500_000


def snapshot_tree_digest(root: Path) -> str:
    configured = root
    if configured.is_symlink():
        raise ValueError("snapshot root must not be a symlink")
    resolved = configured.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("snapshot root must be a directory")
    digest = hashlib.sha256()
    total = 0
    for directory, names, files in os.walk(resolved, followlinks=False):
        current = Path(directory)
        names.sort()
        files.sort()
        if any((current / name).is_symlink() for name in names):
            raise ValueError("snapshot must not contain symlinks")
        for name in files:
            path = current / name
            if path.is_symlink():
                raise ValueError("snapshot must not contain symlinks")
            candidate = path.resolve(strict=True)
            if not candidate.is_relative_to(resolved) or not candidate.is_file():
                raise ValueError("snapshot file escaped its root")
            data = candidate.read_bytes()
            if len(data) > MAX_EVALUATION_FILE_BYTES:
                raise ValueError("snapshot file exceeds the v1 per-file limit")
            total += len(data)
            if total > MAX_EVALUATION_SNAPSHOT_BYTES:
                raise ValueError("snapshot exceeds evaluation size limit")
            relative = candidate.relative_to(resolved).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return digest.hexdigest()
