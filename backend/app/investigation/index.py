from __future__ import annotations

import ast
import hashlib
import io
import os
import re
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.ingestion import RepositorySnapshot

from .domain import (
    CodeChunk,
    ImportRecord,
    ReferenceResults,
    RepositoryMap,
    SearchHit,
    SearchResults,
    SourceExcerpt,
    SourceLocation,
    SymbolRecord,
    SymbolResults,
)
from .errors import (
    InvalidQueryError,
    InvalidSourcePathError,
    SourceNotFoundError,
    SourceRangeError,
    UnsafeSnapshotError,
)

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
_PYTHON_EXTENSIONS = {".py", ".pyi"}
_MAX_SEARCH_RESULTS = 50
_DEFAULT_SEARCH_RESULTS = 20
_MAX_QUERY_CHARACTERS = 500
_MAX_READ_LINES = 200
# Later embedding inputs are deterministic, non-overlapping, and never exceed 120 lines.
_MAX_CHUNK_LINES = 120


@dataclass(frozen=True, slots=True)
class _IndexedFile:
    path: str
    lines: tuple[str, ...]
    tree: ast.Module | None
    decorator_tokens: tuple[tuple[int, int], ...] = ()

    @property
    def text(self) -> str:
        return "".join(self.lines)


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        module: str,
        path: str,
        lines: tuple[str, ...],
        commit_sha: str,
        decorator_tokens: tuple[tuple[int, int], ...],
    ) -> None:
        self._module = module
        self._path = path
        self._lines = lines
        self._commit_sha = commit_sha
        self._decorator_tokens = decorator_tokens
        self._scope: list[tuple[str, str]] = []
        self.symbols: list[SymbolRecord] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record(node, "class")
        self._scope.append((node.name, "class"))
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self._scope and self._scope[-1][1] == "class" else "function"
        self._record(node, kind)
        self._scope.append((node.name, "function"))
        self.generic_visit(node)
        self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = (
            "async_method"
            if self._scope and self._scope[-1][1] == "class"
            else "async_function"
        )
        self._record(node, kind)
        self._scope.append((node.name, "function"))
        self.generic_visit(node)
        self._scope.pop()

    def _record(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: str,
    ) -> None:
        qualified_parts = [self._module, *(name for name, _ in self._scope), node.name]
        self.symbols.append(
            SymbolRecord(
                name=node.name,
                qualified_name=".".join(part for part in qualified_parts if part),
                kind=kind,
                source=_excerpt(
                    self._path,
                    self._lines,
                    _definition_start_line(node, self._decorator_tokens),
                    node.end_lineno or node.lineno,
                    self._commit_sha,
                ),
            )
        )


class PythonRepositoryIndex:
    """Immutable in-memory index over one safely extracted repository snapshot."""

    def __init__(
        self,
        *,
        snapshot: RepositorySnapshot,
        files: tuple[_IndexedFile, ...],
        symbols: tuple[SymbolRecord, ...],
        imports: tuple[ImportRecord, ...],
        repository_map: RepositoryMap,
        chunks: tuple[CodeChunk, ...],
    ) -> None:
        self.snapshot = snapshot
        self._files = files
        self._files_by_path = {item.path: item for item in files}
        self.symbols = symbols
        self.imports = imports
        self.repository_map = repository_map
        self.chunks = chunks

    @classmethod
    def build(cls, snapshot: RepositorySnapshot) -> PythonRepositoryIndex:
        files = _read_snapshot(snapshot)
        symbols: list[SymbolRecord] = []
        imports: list[ImportRecord] = []
        parse_errors: list[str] = []
        parsed_files: list[_IndexedFile] = []

        for item in files:
            suffix = PurePosixPath(item.path).suffix.casefold()
            if suffix not in _PYTHON_EXTENSIONS:
                parsed_files.append(item)
                continue
            module = _module_name(item.path)
            try:
                tree = ast.parse(item.text, filename=item.path)
            except SyntaxError:
                parse_errors.append(item.path)
                parsed_files.append(item)
                continue
            parsed = _IndexedFile(
                path=item.path,
                lines=item.lines,
                tree=tree,
                decorator_tokens=_decorator_at_tokens(item.text),
            )
            parsed_files.append(parsed)
            end_line = max(1, len(item.lines))
            symbols.append(
                SymbolRecord(
                    name=module.rsplit(".", 1)[-1],
                    qualified_name=module,
                    kind="module",
                    source=_excerpt(item.path, item.lines, 1, end_line, snapshot.commit_sha),
                )
            )
            visitor = _SymbolVisitor(
                module=module,
                path=item.path,
                lines=item.lines,
                commit_sha=snapshot.commit_sha,
                decorator_tokens=parsed.decorator_tokens,
            )
            visitor.visit(tree)
            symbols.extend(visitor.symbols)
            imports.extend(_extract_imports(parsed, snapshot.commit_sha))

        ordered_files = tuple(sorted(parsed_files, key=lambda item: item.path))
        ordered_symbols = tuple(
            sorted(
                symbols,
                key=lambda item: (
                    item.source.location.path,
                    item.source.location.start_line,
                    item.qualified_name,
                    item.kind,
                ),
            )
        )
        ordered_imports = tuple(
            sorted(
                imports,
                key=lambda item: (
                    item.source.location.path,
                    item.source.location.start_line,
                    item.module,
                    item.name or "",
                    item.alias or "",
                ),
            )
        )
        repository_map = _build_repository_map(
            snapshot, ordered_files, ordered_symbols, ordered_imports, tuple(parse_errors)
        )
        chunks = _build_chunks(snapshot, ordered_files)
        return cls(
            snapshot=snapshot,
            files=ordered_files,
            symbols=ordered_symbols,
            imports=ordered_imports,
            repository_map=repository_map,
            chunks=chunks,
        )

    def search_code(
        self,
        query: str,
        path_prefix: str | None = None,
        *,
        limit: int = _DEFAULT_SEARCH_RESULTS,
    ) -> SearchResults:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > _MAX_QUERY_CHARACTERS
        ):
            raise InvalidQueryError("Search query must contain 1 to 500 characters.")
        if type(limit) is not int or not 1 <= limit <= _MAX_SEARCH_RESULTS:
            raise InvalidQueryError("Search result limit is outside the supported range.")
        normalized_prefix = (
            _normalize_relative_path(path_prefix, allow_trailing=True)
            if path_prefix is not None
            else None
        )
        folded_query = query.casefold()
        hits: list[SearchHit] = []
        for item in self._files:
            if normalized_prefix is not None and not (
                item.path == normalized_prefix
                or item.path.startswith(f"{normalized_prefix}/")
            ):
                continue
            path_score = 25 if folded_query in item.path.casefold() else 0
            for line_number, line in enumerate(item.lines, start=1):
                occurrences = line.casefold().count(folded_query)
                if occurrences:
                    hits.append(
                        SearchHit(
                            source=_excerpt(
                                item.path,
                                item.lines,
                                line_number,
                                line_number,
                                self.snapshot.commit_sha,
                            ),
                            score=occurrences * 100 + path_score,
                            match_kind="lexical",
                            certainty="literal",
                        )
                    )
        hits.sort(
            key=lambda hit: (
                -hit.score,
                hit.source.location.path,
                hit.source.location.start_line,
                hit.source.excerpt,
            )
        )
        return SearchResults(query=query, hits=tuple(hits[:limit]))

    def read_code(self, path: str, start_line: int, end_line: int) -> SourceExcerpt:
        normalized = _normalize_relative_path(path)
        item = self._files_by_path.get(normalized)
        if item is None:
            raise SourceNotFoundError("The requested retained source path was not found.")
        if (
            type(start_line) is not int
            or type(end_line) is not int
            or start_line < 1
            or end_line < start_line
            or end_line - start_line + 1 > _MAX_READ_LINES
            or end_line > len(item.lines)
        ):
            raise SourceRangeError("The requested source line range is invalid.")
        return _excerpt(
            item.path, item.lines, start_line, end_line, self.snapshot.commit_sha
        )

    def find_symbol(self, symbol: str) -> SymbolResults:
        normalized = _validate_symbol_query(symbol)
        if "." in normalized:
            matches = tuple(item for item in self.symbols if item.qualified_name == normalized)
        else:
            matches = tuple(item for item in self.symbols if item.name == normalized)
        return SymbolResults(symbol=normalized, symbols=matches)

    def find_references(self, symbol: str) -> ReferenceResults:
        normalized = _validate_symbol_query(symbol)
        hits: list[SearchHit] = []
        occupied: set[tuple[str, int]] = set()
        definitions = self.find_symbol(normalized).symbols
        for definition in definitions:
            if definition.kind == "module":
                continue
            location = definition.source.location
            occupied.add((location.path, location.start_line))
            hits.append(
                SearchHit(
                    source=_excerpt(
                        location.path,
                        self._files_by_path[location.path].lines,
                        location.start_line,
                        location.start_line,
                        self.snapshot.commit_sha,
                    ),
                    score=300,
                    match_kind="definition",
                    certainty="definition",
                )
            )

        unqualified = "." not in normalized
        for item in self._files:
            if item.tree is None:
                continue
            for node in ast.walk(item.tree):
                matched = False
                if isinstance(node, ast.Name):
                    matched = unqualified and node.id == normalized
                elif isinstance(node, ast.Attribute):
                    dotted = _attribute_name(node)
                    matched = dotted == normalized or (
                        unqualified and node.attr == normalized
                    )
                if not matched or not hasattr(node, "lineno"):
                    continue
                line_number = node.lineno
                key = (item.path, line_number)
                if key in occupied:
                    continue
                occupied.add(key)
                hits.append(
                    SearchHit(
                        source=_excerpt(
                            item.path,
                            item.lines,
                            line_number,
                            line_number,
                            self.snapshot.commit_sha,
                        ),
                        score=200,
                        match_kind="ast_reference",
                        certainty="static_syntax",
                    )
                )

        pattern = re.compile(rf"(?<!\w){re.escape(normalized)}(?!\w)")
        for item in self._files:
            for line_number, line in enumerate(item.lines, start=1):
                key = (item.path, line_number)
                if key in occupied or not pattern.search(line):
                    continue
                occupied.add(key)
                hits.append(
                    SearchHit(
                        source=_excerpt(
                            item.path,
                            item.lines,
                            line_number,
                            line_number,
                            self.snapshot.commit_sha,
                        ),
                        score=100,
                        match_kind="text_reference",
                        certainty="conservative_text",
                    )
                )
        kind_order = {"definition": 0, "ast_reference": 1, "text_reference": 2}
        hits.sort(
            key=lambda hit: (
                hit.source.location.path,
                hit.source.location.start_line,
                kind_order[hit.match_kind],
                hit.source.excerpt,
            )
        )
        return ReferenceResults(symbol=normalized, hits=tuple(hits))


def _read_snapshot(snapshot: RepositorySnapshot) -> tuple[_IndexedFile, ...]:
    configured_root = snapshot.root_path
    if configured_root.is_symlink():
        raise UnsafeSnapshotError("Snapshot root must not be a symlink.")
    try:
        root = configured_root.resolve(strict=True)
    except OSError as exc:
        raise UnsafeSnapshotError("Snapshot root is unavailable.") from exc
    if not root.is_dir():
        raise UnsafeSnapshotError("Snapshot root must be a directory.")

    indexed: list[_IndexedFile] = []
    try:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            current = Path(directory)
            directory_names.sort()
            file_names.sort()
            for name in directory_names:
                if (current / name).is_symlink():
                    raise UnsafeSnapshotError("Snapshot directories must not be symlinks.")
            for name in file_names:
                candidate = current / name
                if candidate.is_symlink():
                    raise UnsafeSnapshotError("Snapshot files must not be symlinks.")
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    raise UnsafeSnapshotError("Snapshot file escaped the configured root.")
                relative = resolved.relative_to(root).as_posix()
                if resolved.suffix.casefold() not in _RETAINED_EXTENSIONS:
                    continue
                text = resolved.read_bytes().decode("utf-8")
                lines = tuple(text.splitlines(keepends=True)) or ("",)
                indexed.append(
                    _IndexedFile(
                        path=relative,
                        lines=lines,
                        tree=None,
                    )
                )
    except (OSError, UnicodeError) as exc:
        raise UnsafeSnapshotError("Snapshot contents could not be read safely.") from exc
    return tuple(sorted(indexed, key=lambda item: item.path))


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(parts)


def _excerpt(
    path: str,
    lines: tuple[str, ...],
    start_line: int,
    end_line: int,
    commit_sha: str,
) -> SourceExcerpt:
    return SourceExcerpt(
        location=SourceLocation(
            path=path,
            start_line=start_line,
            end_line=end_line,
            commit_sha=commit_sha,
        ),
        excerpt="".join(lines[start_line - 1 : end_line]),
    )


def _extract_imports(item: _IndexedFile, commit_sha: str) -> list[ImportRecord]:
    assert item.tree is not None
    records: list[ImportRecord] = []
    for node in ast.walk(item.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                records.append(
                    ImportRecord(
                        module=alias.name,
                        alias=alias.asname,
                        source=_excerpt(
                            item.path,
                            item.lines,
                            node.lineno,
                            node.end_lineno or node.lineno,
                            commit_sha,
                        ),
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                records.append(
                    ImportRecord(
                        module=module,
                        name=alias.name,
                        alias=alias.asname,
                        source=_excerpt(
                            item.path,
                            item.lines,
                            node.lineno,
                            node.end_lineno or node.lineno,
                            commit_sha,
                        ),
                    )
                )
    return records


def _build_repository_map(
    snapshot: RepositorySnapshot,
    files: tuple[_IndexedFile, ...],
    symbols: tuple[SymbolRecord, ...],
    imports: tuple[ImportRecord, ...],
    parse_errors: tuple[str, ...],
) -> RepositoryMap:
    paths = tuple(item.path for item in files)
    python_paths = tuple(
        path for path in paths if PurePosixPath(path).suffix.casefold() in _PYTHON_EXTENSIONS
    )
    modules = tuple(sorted({_module_name(path) for path in python_paths}))
    packages = tuple(
        sorted(
            {
                _module_name(path)
                for path in python_paths
                if PurePosixPath(path).name in {"__init__.py", "__init__.pyi"}
            }
        )
    )
    test_files = tuple(
        sorted(
            path
            for path in python_paths
            if "tests" in PurePosixPath(path).parts
            or PurePosixPath(path).name.startswith("test_")
            or PurePosixPath(path).stem.endswith("_test")
        )
    )
    counts = Counter(item.kind for item in symbols)
    return RepositoryMap(
        commit_sha=snapshot.commit_sha,
        top_level_entries=tuple(sorted({path.split("/", 1)[0] for path in paths})),
        python_modules=modules,
        python_packages=packages,
        test_files=test_files,
        context_files=tuple(sorted(path for path in paths if path not in python_paths)),
        symbol_counts=tuple(sorted(counts.items())),
        imports=imports,
        parse_error_paths=tuple(sorted(parse_errors)),
    )


def _build_chunks(
    snapshot: RepositorySnapshot, files: tuple[_IndexedFile, ...]
) -> tuple[CodeChunk, ...]:
    chunks: list[CodeChunk] = []
    for item in files:
        if not item.text:
            continue
        boundaries = {1, len(item.lines) + 1}
        if item.tree is not None:
            boundaries.update(
                _definition_start_line(node, item.decorator_tokens)
                for node in ast.walk(item.tree)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            )
        ordered = sorted(boundaries)
        ranges: list[tuple[int, int]] = []
        for boundary_index in range(len(ordered) - 1):
            start = ordered[boundary_index]
            segment_end = ordered[boundary_index + 1] - 1
            while start <= segment_end:
                end = min(segment_end, start + _MAX_CHUNK_LINES - 1)
                ranges.append((start, end))
                start = end + 1
        suffix = PurePosixPath(item.path).suffix.casefold()
        language = {
            ".py": "python",
            ".pyi": "python",
            ".md": "markdown",
            ".rst": "rst",
            ".toml": "toml",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".json": "json",
            ".txt": "text",
        }[suffix]
        context_kind = "code" if suffix in _PYTHON_EXTENSIONS else "context"
        for start, end in ranges:
            source = _excerpt(item.path, item.lines, start, end, snapshot.commit_sha)
            identity = "\0".join(
                [snapshot.commit_sha, item.path, str(start), str(end), source.excerpt]
            ).encode("utf-8")
            chunks.append(
                CodeChunk(
                    chunk_id=hashlib.sha256(identity).hexdigest(),
                    source=source,
                    text=source.excerpt,
                    language=language,
                    context_kind=context_kind,
                )
            )
    return tuple(chunks)


def _normalize_relative_path(path: str, *, allow_trailing: bool = False) -> str:
    if (
        not isinstance(path, str)
        or path != path.strip()
        or not path
        or "\x00" in path
    ):
        raise InvalidSourcePathError("Source paths must be non-empty relative paths.")
    normalized = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", normalized) or normalized.startswith("/"):
        raise InvalidSourcePathError("Absolute source paths are not allowed.")
    if allow_trailing:
        normalized = normalized.rstrip("/")
    pure = PurePosixPath(normalized)
    if not normalized or any(part in {"", ".", ".."} for part in pure.parts):
        raise InvalidSourcePathError("Source path traversal is not allowed.")
    return pure.as_posix()


def _validate_symbol_query(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        raise InvalidQueryError("Symbol query must not be empty.")
    return symbol


def _definition_start_line(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    decorator_tokens: tuple[tuple[int, int], ...],
) -> int:
    decorator_count = len(node.decorator_list)
    if decorator_count == 0:
        return node.lineno
    candidates = tuple(
        line
        for line, column in decorator_tokens
        if column == node.col_offset and line < node.lineno
    )
    if len(candidates) < decorator_count:
        raise UnsafeSnapshotError("Decorated definition tokens are inconsistent.")
    return candidates[-decorator_count]


def _decorator_at_tokens(text: str) -> tuple[tuple[int, int], ...]:
    markers: list[tuple[int, int]] = []
    logical_line_has_code = False
    ignored = {
        tokenize.COMMENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.NL,
    }
    token_source = io.StringIO(text, newline=None)
    for token in tokenize.generate_tokens(token_source.readline):
        if token.type == tokenize.NEWLINE:
            logical_line_has_code = False
            continue
        if token.type in ignored:
            continue
        if (
            not logical_line_has_code
            and token.type == tokenize.OP
            and token.string == "@"
        ):
            markers.append(token.start)
        logical_line_has_code = True
    return tuple(markers)


def _attribute_name(node: ast.Attribute) -> str:
    parts = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))
