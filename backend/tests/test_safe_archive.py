from __future__ import annotations

import io
import stat
import zipfile
from collections.abc import Mapping

import pytest

from app.ingestion import SafeArchiveExtractor, SourceLimitError, UnsafeArchiveError


def _zip_bytes(
    entries: Mapping[str, bytes], *, symlinks: Mapping[str, str] | None = None
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


def test_extractor_retains_only_static_text_context_and_counts_written_bytes(
    tmp_path,
) -> None:
    """Breaks if ignored, binary, or unsupported files enter the static snapshot."""
    kept = {
        "repo-sha/src/main.py": b"print('context only')\n",
        "repo-sha/src/types.pyi": b"value: int\n",
        "repo-sha/pyproject.toml": b"[project]\nname='demo'\n",
        "repo-sha/docs/readme.md": b"# Demo\n",
        "repo-sha/config.yaml": b"enabled: true\n",
        "repo-sha/data.json": b'{"kind":"fixture"}\n',
        "repo-sha/.github/workflows/ci.yml": b"name: CI\n",
    }
    ignored = {
        "repo-sha/.git/config.py": b"do_not_keep = True\n",
        "repo-sha/.github/workflows/check.py": b"do_not_keep = True\n",
        "repo-sha/.github/CONTRIBUTING.md": b"do not keep\n",
        "repo-sha/.venv/lib/site.py": b"do_not_keep = True\n",
        "repo-sha/venv/lib/site.py": b"do_not_keep = True\n",
        "repo-sha/node_modules/pkg/index.py": b"do_not_keep = True\n",
        "repo-sha/dist/generated.py": b"do_not_keep = True\n",
        "repo-sha/build/generated.py": b"do_not_keep = True\n",
        "repo-sha/vendor/pkg.py": b"do_not_keep = True\n",
        "repo-sha/src/__pycache__/cached.py": b"do_not_keep = True\n",
        "repo-sha/.mypy_cache/cache.py": b"do_not_keep = True\n",
        "repo-sha/.pytest_cache/cache.py": b"do_not_keep = True\n",
        "repo-sha/.ruff_cache/cache.py": b"do_not_keep = True\n",
        "repo-sha/src/image.png": b"\x89PNG\r\n",
        "repo-sha/src/extensionless": b"not retained\n",
        "repo-sha/src/binary.py": b"prefix\x00suffix",
        "repo-sha/src/non_utf8.py": b"\xff\xfe",
    }
    destination = tmp_path / "snapshot"

    result = SafeArchiveExtractor().extract(
        _zip_bytes({**kept, **ignored}), destination
    )

    actual_files = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert actual_files == {
        name.removeprefix("repo-sha/"): content for name, content in kept.items()
    }
    assert result.indexed_byte_count == sum(len(content) for content in kept.values())
    assert result.relative_files == tuple(sorted(actual_files))


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "/absolute.py",
        "\\absolute.py",
        "C:/drive.py",
        "C:\\drive.py",
        "repo-sha/../escape.py",
        "repo-sha/src/../../escape.py",
        "repo-sha/src\\..\\escape.py",
    ],
)
def test_extractor_rejects_paths_that_can_escape_destination(
    tmp_path, unsafe_name: str
) -> None:
    """Breaks if ZIP Slip or Windows-qualified archive paths can write out of scope."""
    destination = tmp_path / "snapshot"

    with pytest.raises(UnsafeArchiveError):
        SafeArchiveExtractor().extract(_zip_bytes({unsafe_name: b"unsafe\n"}), destination)

    assert not (tmp_path / "escape.py").exists()


def test_extractor_rejects_nul_in_raw_archive_name(tmp_path) -> None:
    """Breaks if a NUL-truncated ZIP name can disguise the resolved target."""
    archive = _zip_bytes({"repo-sha/xevil.py": b"unsafe\n"})
    archive = archive.replace(b"xevil.py", b"\x00evil.py")

    with pytest.raises(UnsafeArchiveError):
        SafeArchiveExtractor().extract(archive, tmp_path / "snapshot")


def test_extractor_rejects_symlink_entries_even_when_extension_is_ignored(tmp_path) -> None:
    """Breaks if an archive link can redirect a later write outside the snapshot."""
    archive = _zip_bytes(
        {"repo-sha/src/main.py": b"safe = True\n"},
        symlinks={"repo-sha/assets/ignored.bin": "../../outside"},
    )

    with pytest.raises(UnsafeArchiveError):
        SafeArchiveExtractor().extract(archive, tmp_path / "snapshot")


def test_extractor_maps_file_directory_collisions_to_an_unsafe_archive_error(
    tmp_path,
) -> None:
    """Breaks if conflicting archive targets escape as raw filesystem failures."""
    archive = _zip_bytes(
        {
            "repo-sha/src.py": b"module = True\n",
            "repo-sha/src.py/nested.py": b"nested = True\n",
        }
    )

    with pytest.raises(UnsafeArchiveError):
        SafeArchiveExtractor().extract(archive, tmp_path / "snapshot")


def test_extractor_accepts_a_retained_file_at_exact_500_000_byte_limit(tmp_path) -> None:
    """Breaks if the documented per-file ceiling rejects its exact boundary."""
    content = b"a" * 500_000

    result = SafeArchiveExtractor().extract(
        _zip_bytes({"repo-sha/large.py": content}), tmp_path / "snapshot"
    )

    assert result.indexed_byte_count == 500_000


def test_extractor_rejects_a_retained_file_above_500_000_bytes(tmp_path) -> None:
    """Breaks if one source file can exceed the per-file extraction budget."""
    archive = _zip_bytes({"repo-sha/large.py": b"a" * 500_001})

    with pytest.raises(SourceLimitError):
        SafeArchiveExtractor().extract(archive, tmp_path / "snapshot")


def test_extractor_accepts_total_retained_bytes_at_exact_10_000_000_limit(
    tmp_path,
) -> None:
    """Breaks if the aggregate retained-byte ceiling rejects its exact boundary."""
    entries = {
        f"repo-sha/src/file_{index}.py": b"a" * 500_000
        for index in range(20)
    }

    result = SafeArchiveExtractor().extract(
        _zip_bytes(entries), tmp_path / "snapshot"
    )

    assert result.indexed_byte_count == 10_000_000


def test_extractor_rejects_total_retained_bytes_above_10_000_000(tmp_path) -> None:
    """Breaks if many individually valid files can exceed the total source budget."""
    entries = {
        **{
            f"repo-sha/src/file_{index}.py": b"a" * 500_000
            for index in range(20)
        },
        "repo-sha/src/overflow.py": b"a",
    }

    with pytest.raises(SourceLimitError):
        SafeArchiveExtractor().extract(_zip_bytes(entries), tmp_path / "snapshot")


def test_extractor_rejects_malformed_zip_bytes(tmp_path) -> None:
    """Breaks if corrupt upstream bytes escape as raw zipfile exceptions."""
    with pytest.raises(UnsafeArchiveError):
        SafeArchiveExtractor().extract(b"not a zip", tmp_path / "snapshot")
