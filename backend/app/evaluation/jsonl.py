from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .contracts import EVALUATION_JSONL_ROW_MAX_BYTES


RowT = TypeVar("RowT", bound=BaseModel)
MAX_JSONL_BYTES = 10 * 1024 * 1024
MAX_JSONL_LINE_BYTES = EVALUATION_JSONL_ROW_MAX_BYTES


class JsonlContractError(ValueError):
    pass


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JsonlContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise JsonlContractError(f"non-finite JSON number: {value}")


def read_jsonl(path: Path, model: type[RowT]) -> tuple[RowT, ...]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise JsonlContractError("JSONL file is unavailable") from exc
    if len(data) > MAX_JSONL_BYTES:
        raise JsonlContractError("JSONL file is too large")
    if data.startswith(b"\xef\xbb\xbf"):
        raise JsonlContractError("JSONL must not contain a BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonlContractError("JSONL must be UTF-8") from exc
    rows: list[RowT] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise JsonlContractError(f"blank JSONL line at {line_number}")
        if len(line.encode("utf-8")) > MAX_JSONL_LINE_BYTES:
            raise JsonlContractError(f"JSONL line {line_number} is too large")
        try:
            raw = json.loads(
                line,
                object_pairs_hook=_pairs,
                parse_constant=_reject_constant,
            )
            rows.append(model.model_validate(raw))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise JsonlContractError(f"invalid JSONL row {line_number}") from exc
    identities = [_identity(item) for item in rows]
    if len(set(identities)) != len(identities):
        raise JsonlContractError("JSONL row identities must be unique")
    return tuple(rows)


def write_jsonl(path: Path, rows: tuple[BaseModel, ...]) -> None:
    identities = [_identity(item) for item in rows]
    if len(set(identities)) != len(identities):
        raise JsonlContractError("JSONL row identities must be unique")
    ordered = sorted(rows, key=_identity)
    records = [
        json.dumps(
            item.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for item in ordered
    ]
    if any(len(record.encode("utf-8")) > MAX_JSONL_LINE_BYTES for record in records):
        raise JsonlContractError("JSONL output row is too large")
    payload = "".join(f"{record}\n" for record in records).encode("utf-8")
    if len(payload) > MAX_JSONL_BYTES:
        raise JsonlContractError("JSONL output is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _identity(item: BaseModel) -> tuple[str, ...]:
    case_id = getattr(item, "case_id", None)
    system = getattr(item, "system", None)
    if isinstance(case_id, str) and isinstance(system, str):
        return (case_id, system)
    if isinstance(case_id, str):
        return (case_id,)
    slot_id = getattr(item, "slot_id", None)
    if isinstance(slot_id, str):
        return (slot_id,)
    raise JsonlContractError("JSONL row has no supported identity")
