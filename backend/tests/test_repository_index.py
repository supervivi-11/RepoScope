from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ingestion import RepositorySnapshot
from app.investigation import (
    InvalidQueryError,
    InvalidSourcePathError,
    PythonRepositoryIndex,
    SourceNotFoundError,
    SourceRangeError,
    UnsafeSnapshotError,
)


SHA = "a" * 40


def _snapshot(root: Path) -> RepositorySnapshot:
    return RepositorySnapshot.create(
        owner="octo",
        repository="demo",
        commit_sha=SHA,
        root_path=root,
        indexed_byte_count=sum(
            path.stat().st_size for path in root.rglob("*") if path.is_file()
        ),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def _build(tmp_path: Path, files: dict[str, str]) -> PythonRepositoryIndex:
    root = tmp_path / "snapshot"
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    return PythonRepositoryIndex.build(_snapshot(root))


def test_index_extracts_nested_async_symbols_imports_and_deterministic_map(tmp_path) -> None:
    """Breaks if AST metadata loses nesting, async/method kinds, imports, or sorting."""
    index = _build(
        tmp_path,
        {
            "src/pkg/__init__.py": "from .worker import Worker\n",
            "src/pkg/worker.py": (
                "import os as operating_system\n"
                "from collections import defaultdict as dd\n"
                "\n"
                "class Worker:\n"
                "    async def run(self):\n"
                "        def nested():\n"
                "            return dd()\n"
                "        return nested()\n"
                "\n"
                "async def top_level():\n"
                "    return Worker()\n"
            ),
            "tests/test_worker.py": "def test_worker():\n    assert True\n",
            "README.md": "Worker guide\n",
            "pyproject.toml": "[project]\nname = 'demo'\n",
        },
    )

    symbols = index.symbols
    assert [(item.qualified_name, item.kind) for item in symbols] == [
        ("src.pkg", "module"),
        ("src.pkg.worker", "module"),
        ("src.pkg.worker.Worker", "class"),
        ("src.pkg.worker.Worker.run", "async_method"),
        ("src.pkg.worker.Worker.run.nested", "function"),
        ("src.pkg.worker.top_level", "async_function"),
        ("tests.test_worker", "module"),
        ("tests.test_worker.test_worker", "function"),
    ]
    run = next(item for item in symbols if item.qualified_name.endswith("Worker.run"))
    assert run.source.location.path == "src/pkg/worker.py"
    assert (run.source.location.start_line, run.source.location.end_line) == (5, 8)
    assert run.source.excerpt == (
        "    async def run(self):\n"
        "        def nested():\n"
        "            return dd()\n"
        "        return nested()\n"
    )
    assert run.source.location.commit_sha == SHA

    assert [(item.module, item.name, item.alias) for item in index.imports] == [
        (".worker", "Worker", None),
        ("os", None, "operating_system"),
        ("collections", "defaultdict", "dd"),
    ]
    repository_map = index.repository_map
    assert repository_map.top_level_entries == (
        "README.md",
        "pyproject.toml",
        "src",
        "tests",
    )
    assert repository_map.python_modules == (
        "src.pkg",
        "src.pkg.worker",
        "tests.test_worker",
    )
    assert repository_map.python_packages == ("src.pkg",)
    assert repository_map.test_files == ("tests/test_worker.py",)
    assert repository_map.context_files == ("README.md", "pyproject.toml")
    assert repository_map.parse_error_paths == ()
    assert repository_map.symbol_counts == (
        ("async_function", 1),
        ("async_method", 1),
        ("class", 1),
        ("function", 2),
        ("module", 3),
    )


def test_syntax_error_file_remains_searchable_and_is_reported(tmp_path) -> None:
    """Breaks if one malformed Python file aborts indexing or disappears from search."""
    index = _build(
        tmp_path,
        {
            "broken.py": "def broken(:\n    TOKEN = '雪'\n",
            "healthy.py": "def healthy():\n    return True\n",
        },
    )

    assert index.repository_map.parse_error_paths == ("broken.py",)
    result = index.search_code("雪")
    assert len(result.hits) == 1
    assert result.hits[0].source.location.path == "broken.py"
    assert result.hits[0].source.location.start_line == 2
    assert result.hits[0].source.excerpt == "    TOKEN = '雪'\n"
    assert any(item.qualified_name == "healthy.healthy" for item in index.symbols)


def test_empty_python_module_has_one_consistent_empty_source_line(tmp_path) -> None:
    """Breaks if empty modules publish a source location that cannot be read exactly."""
    index = _build(tmp_path, {"pkg/__init__.py": ""})

    module = index.find_symbol("pkg").symbols[0]
    assert module.source.location.start_line == 1
    assert module.source.location.end_line == 1
    assert module.source.excerpt == ""
    assert index.read_code("pkg/__init__.py", 1, 1) == module.source


def test_python_stubs_are_parsed_without_duplicating_logical_modules(tmp_path) -> None:
    """Breaks if `.pyi` AST symbols vanish or map module names are duplicated."""
    index = _build(
        tmp_path,
        {
            "pkg/api.py": "class Client:\n    pass\n",
            "pkg/api.pyi": "class Client:\n    async def send(self) -> None: ...\n",
        },
    )

    assert index.repository_map.python_modules == ("pkg.api",)
    assert [item.source.location.path for item in index.find_symbol("Client").symbols] == [
        "pkg/api.py",
        "pkg/api.pyi",
    ]
    assert index.find_symbol("send").symbols[0].kind == "async_method"


def test_models_are_immutable_and_reject_mutation(tmp_path) -> None:
    """Breaks if structured tool records can drift after index construction."""
    index = _build(tmp_path, {"main.py": "value = 1\n"})

    with pytest.raises(ValidationError):
        index.repository_map.commit_sha = "b" * 40


def test_build_rejects_snapshot_symlinks(tmp_path, monkeypatch) -> None:
    """Breaks if defensive indexing follows a file link outside the safe snapshot."""
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    link = root / "linked.py"
    try:
        os.symlink(outside, link)
    except OSError:
        # Windows may deny symlink creation without Developer Mode. Preserve the
        # filesystem boundary behavior under test by substituting only metadata.
        link.write_text("placeholder\n", encoding="utf-8")
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda candidate: candidate == link or real_is_symlink(candidate),
        )

    with pytest.raises(UnsafeSnapshotError):
        PythonRepositoryIndex.build(_snapshot(root))


def test_search_is_case_insensitive_ranked_bounded_and_prefix_scoped(tmp_path) -> None:
    """Breaks if lexical ranking, stable limits, Unicode folding, or path scope regresses."""
    index = _build(
        tmp_path,
        {
            "parser/core.py": "Parser parser PARSER\n",
            "src/parser_notes.md": "parser\n",
            "src/other.py": "PARSER parser\n",
        },
    )

    hits = index.search_code("parser", limit=2).hits
    assert [(hit.source.location.path, hit.score) for hit in hits] == [
        ("parser/core.py", 325),
        ("src/other.py", 200),
    ]
    assert [hit.source.location.path for hit in index.search_code("parser", "src").hits] == [
        "src/other.py",
        "src/parser_notes.md",
    ]
    assert index.search_code("strasse", path_prefix="src").hits == ()

    with pytest.raises(InvalidQueryError):
        index.search_code("  ")
    with pytest.raises(InvalidQueryError):
        index.search_code("x" * 501)
    with pytest.raises(InvalidSourcePathError):
        index.search_code("parser", "../outside")


def test_read_code_enforces_exact_lines_paths_and_200_line_boundary(tmp_path) -> None:
    """Breaks if reads escape retained files or line/range limits become ambiguous."""
    content = "".join(f"line {number}\n" for number in range(1, 202))
    index = _build(tmp_path, {"src/long.py": content})

    excerpt = index.read_code("src\\long.py", 1, 200)
    assert excerpt.location.path == "src/long.py"
    assert excerpt.location.start_line == 1
    assert excerpt.location.end_line == 200
    assert excerpt.excerpt.startswith("line 1\n")
    assert excerpt.excerpt.endswith("line 200\n")

    with pytest.raises(SourceRangeError):
        index.read_code("src/long.py", 1, 201)
    with pytest.raises(SourceRangeError):
        index.read_code("src/long.py", 0, 1)
    with pytest.raises(SourceRangeError):
        index.read_code("src/long.py", 2, 1)
    with pytest.raises(SourceRangeError):
        index.read_code("src/long.py", 201, 202)
    with pytest.raises(InvalidSourcePathError):
        index.read_code("C:\\outside.py", 1, 1)
    with pytest.raises(InvalidSourcePathError):
        index.read_code("src/../long.py", 1, 1)
    with pytest.raises(InvalidSourcePathError):
        index.read_code("src/long.py\x00", 1, 1)
    with pytest.raises(SourceNotFoundError):
        index.read_code("missing.py", 1, 1)


def test_source_excerpts_and_chunks_preserve_crlf_and_cr_bytes_exactly(tmp_path) -> None:
    """Breaks if text decoding applies universal-newline translation."""
    index = _build(
        tmp_path,
        {
            "windows.py": "value = 1\r\nnext_value = 2\r\n",
            "legacy.txt": "alpha\rbeta\r",
        },
    )

    assert index.read_code("windows.py", 1, 2).excerpt == (
        "value = 1\r\nnext_value = 2\r\n"
    )
    assert index.read_code("legacy.txt", 1, 2).excerpt == "alpha\rbeta\r"
    chunks = {chunk.source.location.path: chunk for chunk in index.chunks}
    assert chunks["windows.py"].text == "value = 1\r\nnext_value = 2\r\n"
    assert chunks["windows.py"].chunk_id == (
        "cc3d706b9b954725fc7086738a05041debf28284383e66489abf5b682096d2ee"
    )
    assert chunks["legacy.txt"].text == "alpha\rbeta\r"
    assert chunks["legacy.txt"].chunk_id == (
        "0f381954bb8d49b4264893b04ffe08d2fb6c9309317c9ed0cd994c667417e219"
    )


def test_find_symbol_handles_collisions_and_references_are_conservative(tmp_path) -> None:
    """Breaks if exact lookup loses collisions or references claim dynamic certainty."""
    index = _build(
        tmp_path,
        {
            "a.py": (
                "def target():\n"
                "    return 1\n"
                "\n"
                "def caller(obj):\n"
                "    target()\n"
                "    obj.target()\n"
                "    return 'target in text'\n"
            ),
            "b.py": "def target():\n    return 2\n",
            "broken.py": "def nope(:\ntarget()\n",
        },
    )

    assert [item.qualified_name for item in index.find_symbol("target").symbols] == [
        "a.target",
        "b.target",
    ]
    assert [item.qualified_name for item in index.find_symbol("a.target").symbols] == [
        "a.target"
    ]
    references = index.find_references("target").hits
    observed_references = [
        (hit.source.location.path, hit.source.location.start_line, hit.match_kind)
        for hit in references
    ]
    assert observed_references == [
        ("a.py", 1, "definition"),
        ("a.py", 5, "ast_reference"),
        ("a.py", 6, "ast_reference"),
        ("a.py", 7, "text_reference"),
        ("b.py", 1, "definition"),
        ("broken.py", 2, "text_reference"),
    ]
    assert all(hit.certainty != "dynamic_call" for hit in references)


def test_decorated_definition_ranges_begin_at_earliest_decorator(tmp_path) -> None:
    """Breaks if decorated classes, functions, or async functions omit decorators."""
    index = _build(
        tmp_path,
        {
            "decorated.py": (
                "@class_decorator\n"
                "class Service:\n"
                "    @method_decorator\n"
                "    def run(self):\n"
                "        return True\n"
                "\n"
                "@function_decorator\n"
                "def helper():\n"
                "    return 1\n"
                "\n"
                "@first\n"
                "@second()\n"
                "async def load():\n"
                "    return 2\n"
            )
        },
    )

    service = index.find_symbol("Service").symbols[0]
    method = index.find_symbol("run").symbols[0]
    helper = index.find_symbol("helper").symbols[0]
    load = index.find_symbol("load").symbols[0]
    assert (service.source.location.start_line, service.source.location.end_line) == (
        1,
        5,
    )
    assert service.source.excerpt.startswith("@class_decorator\nclass Service:\n")
    assert (method.source.location.start_line, method.source.location.end_line) == (
        3,
        5,
    )
    assert method.source.excerpt.startswith("    @method_decorator\n    def run")
    assert (helper.source.location.start_line, helper.source.location.end_line) == (
        7,
        9,
    )
    assert helper.source.excerpt == "@function_decorator\ndef helper():\n    return 1\n"
    assert (load.source.location.start_line, load.source.location.end_line) == (
        11,
        14,
    )
    assert load.source.excerpt == (
        "@first\n@second()\nasync def load():\n    return 2\n"
    )


def test_chunks_are_bounded_exact_and_stable_on_symbol_boundaries(tmp_path) -> None:
    """Breaks if chunk identity drifts or a long file exceeds its line ceiling."""
    long_body = "".join(f"value_{number} = {number}\n" for number in range(1, 131))
    index_one = _build(
        tmp_path,
        {
            "long.py": long_body + "def endpoint():\n    return 1\n",
            "README.md": "context\n",
        },
    )
    index_two = PythonRepositoryIndex.build(index_one.snapshot)

    assert [chunk.chunk_id for chunk in index_one.chunks] == [
        chunk.chunk_id for chunk in index_two.chunks
    ]
    long_chunks = [chunk for chunk in index_one.chunks if chunk.source.location.path == "long.py"]
    observed_ranges = [
        (chunk.source.location.start_line, chunk.source.location.end_line)
        for chunk in long_chunks
    ]
    assert observed_ranges == [
        (1, 120),
        (121, 130),
        (131, 132),
    ]
    assert all(chunk.source.excerpt == chunk.text for chunk in index_one.chunks)
    assert all(
        chunk.source.location.end_line - chunk.source.location.start_line + 1 <= 120
        for chunk in index_one.chunks
    )
    context = next(chunk for chunk in index_one.chunks if chunk.source.location.path == "README.md")
    assert (context.language, context.context_kind) == ("markdown", "context")


def test_long_classes_split_at_nested_method_boundaries(tmp_path) -> None:
    """Breaks if method boundaries are ignored when preparing bounded code chunks."""
    class_prefix = "class Service:\n" + "".join(
        f"    value_{number} = {number}\n" for number in range(1, 125)
    )
    index = _build(
        tmp_path,
        {"service.py": class_prefix + "    def run(self):\n        return True\n"},
    )

    ranges = [
        (chunk.source.location.start_line, chunk.source.location.end_line)
        for chunk in index.chunks
    ]
    assert ranges == [(1, 120), (121, 125), (126, 127)]


def test_long_classes_split_before_nested_method_decorators(tmp_path) -> None:
    """Breaks if decorated nested symbols split at `def` instead of decorator lines."""
    class_prefix = "class Service:\n" + "".join(
        f"    value_{number} = {number}\n" for number in range(1, 124)
    )
    index = _build(
        tmp_path,
        {
            "service.py": (
                class_prefix
                + "    @trace\n"
                + "    async def run(self):\n"
                + "        return True\n"
            )
        },
    )

    ranges = [
        (chunk.source.location.start_line, chunk.source.location.end_line)
        for chunk in index.chunks
    ]
    assert ranges == [(1, 120), (121, 124), (125, 127)]
    assert index.chunks[-1].text.startswith("    @trace\n    async def run")


def test_multiline_parenthesized_decorators_start_at_physical_at_token(tmp_path) -> None:
    """Breaks if AST expression lines replace the actual decorator-token start."""
    class_prefix = "class Service:\n" + "".join(
        f"    value_{number} = {number}\n" for number in range(1, 124)
    )
    decorated_method = (
        "    @(\n"
        "        trace\n"
        "    )\n"
        "    @(\n"
        "        audit\n"
        "    )\n"
        "    async def run(self):\n"
        "        return True\n"
    )
    index = _build(
        tmp_path,
        {"service.py": class_prefix + decorated_method},
    )

    run = index.find_symbol("run").symbols[0]
    assert (run.source.location.start_line, run.source.location.end_line) == (
        125,
        132,
    )
    assert run.source.excerpt == decorated_method
    ranges = [
        (chunk.source.location.start_line, chunk.source.location.end_line)
        for chunk in index.chunks
    ]
    assert ranges == [(1, 120), (121, 124), (125, 132)]
    assert index.chunks[-1].text == decorated_method


def test_decorator_token_scan_ignores_aligned_matrix_operator(tmp_path) -> None:
    """Breaks if an `@` inside a decorator expression replaces its marker token."""
    source = (
        "@(\n"
        "left\n"
        "@ right\n"
        ")\n"
        "async def run():\n"
        "    return True\n"
    )
    index = _build(tmp_path, {"decorated.py": source})

    run = index.find_symbol("run").symbols[0]
    assert (run.source.location.start_line, run.source.location.end_line) == (1, 6)
    assert run.source.excerpt == source
    assert index.chunks[0].text == source


def test_parenthesized_decorator_tokens_keep_physical_rows_in_cr_source(tmp_path) -> None:
    """Breaks if token rows collapse while exact standalone-CR text is retained."""
    source = (
        "@(\r"
        "trace\r"
        ")\r"
        "@(\r"
        "audit\r"
        ")\r"
        "async def run():\r"
        "    return True\r"
    )
    index = _build(tmp_path, {"decorated.py": source})

    run = index.find_symbol("run").symbols[0]
    assert (run.source.location.start_line, run.source.location.end_line) == (1, 8)
    assert run.source.excerpt == source
    assert index.chunks[0].text == source
