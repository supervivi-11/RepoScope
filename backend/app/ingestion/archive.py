from __future__ import annotations

import io
import ntpath
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import SourceLimitError, UnsafeArchiveError

_RETAINED_EXTENSIONS = {
    ".json",
    ".md",
    ".py",
    ".pyi",
    ".rst",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_IGNORED_DIRECTORIES = {
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
    "venv",
}
_MAX_FILE_BYTES = 500 * 1_024
_MAX_TOTAL_BYTES = 10 * 1_024 * 1_024
_READ_CHUNK_BYTES = 64 * 1_024


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    indexed_byte_count: int
    relative_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ArchiveEntry:
    info: zipfile.ZipInfo
    parts: tuple[str, ...]


class SafeArchiveExtractor:
    """Extract a narrow static-text subset without trusting ZIP metadata sizes."""

    def extract(self, archive_bytes: bytes, destination: Path) -> ExtractionResult:
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                entries = tuple(self._validate_entry(info) for info in archive.infolist())
                return self._extract_validated(archive, entries, destination)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as exc:
            raise UnsafeArchiveError() from exc

    def _extract_validated(
        self,
        archive: zipfile.ZipFile,
        entries: tuple[_ArchiveEntry, ...],
        destination: Path,
    ) -> ExtractionResult:
        file_entries = tuple(entry for entry in entries if not entry.info.is_dir())
        prefix = self._common_archive_root(file_entries)
        root = destination.resolve()
        seen_targets: set[Path] = set()
        retained: list[str] = []
        retained_bytes = 0

        destination.mkdir(parents=True, exist_ok=True)
        for entry in file_entries:
            relative_parts = entry.parts[1:] if prefix else entry.parts
            if not relative_parts or self._is_ignored(relative_parts):
                continue
            relative_path = Path(*relative_parts)
            if relative_path.suffix.casefold() not in _RETAINED_EXTENSIONS:
                continue

            target = (destination / relative_path).resolve()
            if not target.is_relative_to(root) or target in seen_targets:
                raise UnsafeArchiveError()
            seen_targets.add(target)

            content = self._read_retained_file(archive, entry.info)
            if self._is_binary(content):
                continue
            if retained_bytes + len(content) > _MAX_TOTAL_BYTES:
                raise SourceLimitError(
                    "The retained repository source exceeds the 10 MB limit."
                )

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as extracted:
                    extracted.write(content)
            except OSError as exc:
                raise UnsafeArchiveError() from exc
            retained_bytes += len(content)
            retained.append(relative_path.as_posix())

        return ExtractionResult(
            indexed_byte_count=retained_bytes,
            relative_files=tuple(sorted(retained)),
        )

    @staticmethod
    def _validate_entry(info: zipfile.ZipInfo) -> _ArchiveEntry:
        original_name = info.orig_filename
        if not original_name or "\x00" in original_name or info.flag_bits & 0x1:
            raise UnsafeArchiveError()
        normalized = original_name.replace("\\", "/")
        drive, _ = ntpath.splitdrive(normalized)
        path = PurePosixPath(normalized)
        if drive or normalized.startswith("/") or path.is_absolute():
            raise UnsafeArchiveError()
        parts = tuple(part for part in path.parts if part not in {"", "."})
        if not parts or ".." in parts:
            raise UnsafeArchiveError()

        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if stat.S_ISLNK(mode):
            raise UnsafeArchiveError()
        if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise UnsafeArchiveError()
        return _ArchiveEntry(info=info, parts=parts)

    @staticmethod
    def _common_archive_root(entries: tuple[_ArchiveEntry, ...]) -> str | None:
        if not entries or any(len(entry.parts) < 2 for entry in entries):
            return None
        candidate = entries[0].parts[0]
        if all(entry.parts[0] == candidate for entry in entries):
            return candidate
        return None

    @staticmethod
    def _is_ignored(parts: tuple[str, ...]) -> bool:
        return any(part.casefold() in _IGNORED_DIRECTORIES for part in parts[:-1])

    @staticmethod
    def _is_binary(content: bytes) -> bool:
        if b"\x00" in content:
            return True
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False

    @staticmethod
    def _read_retained_file(
        archive: zipfile.ZipFile, info: zipfile.ZipInfo
    ) -> bytes:
        content = bytearray()
        try:
            with archive.open(info, "r") as source:
                while chunk := source.read(_READ_CHUNK_BYTES):
                    content.extend(chunk)
                    if len(content) > _MAX_FILE_BYTES:
                        raise SourceLimitError(
                            "A retained repository file exceeds the 500 KB limit."
                        )
        except RuntimeError as exc:
            raise UnsafeArchiveError() from exc
        return bytes(content)
