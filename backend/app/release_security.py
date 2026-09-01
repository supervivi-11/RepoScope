from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_RULES = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "openai_api_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b", re.IGNORECASE),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}
_MAX_SCAN_BYTES = 5 * 1024 * 1024
_DEVELOPMENT_GOLD_PATH = "evals/development-gold.v1.jsonl"
_PUBLIC_DEVELOPMENT_GOLD = {
    "dateutil-dateutil-issue-926": ("dateutil/tz/tz.py",),
    "hynek-structlog-issue-476": ("src/structlog/_log_levels.py",),
    "pallets-click-issue-2819": ("src/click/core.py",),
    "pallets-flask-issue-2267": ("flask/app.py",),
    "pyinvoke-invoke-issue-533": ("invoke/tasks.py",),
    "tox-dev-platformdirs-issue-207": ("src/platformdirs/unix.py",),
}
_FORBIDDEN_EVALUATION_FIELDS = frozenset(
    {
        "changed_file_metadata",
        "changed_paths",
        "fix_commit_sha",
        "fix_first_commit_sha",
        "fix_merge_commit_sha",
        "fix_pr_url",
        "gold_files",
        "gold_production_paths",
    }
)


@dataclass(frozen=True)
class SecretFinding:
    source: str
    path: str
    line: int
    rule: str
    sha256: str


@dataclass(frozen=True)
class SecretAllowlist:
    entries: frozenset[tuple[str, str, str]] = frozenset()

    @classmethod
    def load(cls, path: Path) -> "SecretAllowlist":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("entries"), list):
            raise ValueError("Unsupported secret allowlist schema.")
        entries: set[tuple[str, str, str]] = set()
        for entry in payload["entries"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
                raise ValueError("Every allowlist entry requires a review reason.")
            path_value = str(entry.get("path", "")).replace("\\", "/")
            rule = str(entry.get("rule", ""))
            digest = str(entry.get("sha256", "")).lower()
            if rule not in _RULES or not re.fullmatch(r"[0-9a-f]{64}", digest) or not path_value:
                raise ValueError("Invalid secret allowlist entry.")
            entries.add((path_value, rule, digest))
        return cls(frozenset(entries))

    def permits(self, path: str, rule: str, digest: str) -> bool:
        return (path.replace("\\", "/"), rule, digest) in self.entries


def scan_bytes(
    source: str,
    path: str,
    content: bytes,
    allowlist: SecretAllowlist | None = None,
) -> list[SecretFinding]:
    normalized = path.replace("\\", "/")
    if len(content) > _MAX_SCAN_BYTES:
        return [
            SecretFinding(
                source,
                normalized,
                0,
                "scan_limit_exceeded",
                hashlib.sha256(content).hexdigest(),
            )
        ]
    if b"\x00" in content[:8_192]:
        return []
    text = content.decode("utf-8", errors="replace")
    permitted = allowlist or SecretAllowlist()
    findings: list[SecretFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in _RULES.items():
            for match in pattern.finditer(line):
                digest = hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()
                if not permitted.permits(normalized, rule, digest):
                    findings.append(SecretFinding(source, normalized, line_number, rule, digest))
    return findings


def _git(root: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )


def scan_git_history(root: Path, allowlist: SecretAllowlist | None = None) -> list[SecretFinding]:
    object_paths: dict[str, set[str]] = {}
    commits = _git(root, "rev-list", "--all").stdout.splitlines()
    for commit in commits:
        entries = _git(root, "ls-tree", "-r", "--full-tree", commit).stdout.splitlines()
        for entry in entries:
            metadata, separator, path = entry.partition("\t")
            parts = metadata.split()
            if separator and len(parts) == 3 and parts[1] == "blob" and path:
                object_paths.setdefault(parts[2], set()).add(path)
    if not object_paths:
        return []
    historical_paths = {
        path.replace("\\", "/")
        for paths in object_paths.values()
        for path in paths
    }
    findings = [
        SecretFinding(
            "git-history",
            path,
            0,
            "forbidden_history_path",
            hashlib.sha256(path.encode("utf-8")).hexdigest(),
        )
        for path in forbidden_tracked_paths(historical_paths)
    ]
    checks = _git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_text="\n".join(object_paths) + "\n",
    ).stdout.splitlines()
    for check in checks:
        parts = check.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        object_id = parts[0]
        if int(parts[2]) > _MAX_SCAN_BYTES:
            for path in object_paths[object_id]:
                findings.append(
                    SecretFinding(
                        "git-history",
                        path.replace("\\", "/"),
                        0,
                        "scan_limit_exceeded",
                        hashlib.sha256(object_id.encode("ascii")).hexdigest(),
                    )
                )
            continue
        content = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
        for path in object_paths[object_id]:
            findings.extend(scan_bytes("git-history", path, content, allowlist))
            findings.extend(scan_evaluation_bytes("git-history", path, content))
    return findings


def scan_git_index(
    root: Path, allowlist: SecretAllowlist | None = None
) -> list[SecretFinding]:
    output = subprocess.run(
        ["git", "ls-files", "-s", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8", errors="replace")
    findings: list[SecretFinding] = []
    for entry in output.split("\0"):
        metadata, separator, path = entry.partition("\t")
        parts = metadata.split()
        if not separator or len(parts) != 3 or parts[2] != "0":
            continue
        content = subprocess.run(
            ["git", "cat-file", "blob", parts[1]],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
        findings.extend(scan_bytes("git-index", path, content, allowlist))
        findings.extend(scan_evaluation_bytes("git-index", path, content))
    return findings


def scan_files(
    root: Path,
    files: Iterable[Path],
    source: str,
    allowlist: SecretAllowlist | None = None,
) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for file in files:
        if file.is_file() and not file.is_symlink():
            findings.extend(scan_bytes(source, file.relative_to(root).as_posix(), file.read_bytes(), allowlist))
    return findings


def tracked_paths(root: Path) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8", errors="replace")
    return [item.replace("\\", "/") for item in output.split("\0") if item]


def forbidden_tracked_paths(paths: Iterable[str]) -> list[str]:
    forbidden: list[str] = []
    for raw_path in paths:
        path = raw_path.replace("\\", "/")
        folded = path.casefold()
        name = path.rsplit("/", 1)[-1].lower()
        if "hidden-gold" in folded:
            forbidden.append(path)
        elif name == ".env" or name.endswith((".sqlite", ".sqlite3", ".db")):
            forbidden.append(path)
        elif folded.startswith(("local/", "dist/", "frontend/dist/", "test-results/", "frontend/test-results/", "playwright-report/", "frontend/playwright-report/")):
            forbidden.append(path)
        elif folded.startswith("evals/gold/") or folded.startswith("evals/local-results/"):
            forbidden.append(path)
    return sorted(set(forbidden))


def forbidden_evaluation_artifacts(root: Path, paths: Iterable[str]) -> list[str]:
    """Reject repair-answer fields from public, runner-safe evaluation inputs."""

    violations: set[str] = set()
    for raw_path in paths:
        path = raw_path.replace("\\", "/")
        if not _is_protected_evaluation_jsonl(path):
            continue
        file_path = root / Path(path)
        if not file_path.is_file():
            continue
        for rule in scan_evaluation_bytes("tracked", path, file_path.read_bytes()):
            field = rule.rule.removeprefix("forbidden_evaluation_field_")
            violations.add(f"{path}:{field}")
    return sorted(violations)


def scan_evaluation_bytes(
    source: str, path: str, content: bytes
) -> list[SecretFinding]:
    normalized = path.replace("\\", "/")
    if not _is_protected_evaluation_jsonl(normalized):
        return []
    if len(content) > _MAX_SCAN_BYTES:
        return [
            _evaluation_finding(source, normalized, 0, "scan_limit_exceeded")
        ]
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return [_evaluation_finding(source, normalized, 0, "invalid_utf8")]
    if normalized == _DEVELOPMENT_GOLD_PATH:
        if _valid_development_gold(text):
            return []
        return [
            _evaluation_finding(
                source, normalized, 0, "development_gold_contract"
            )
        ]
    findings: list[SecretFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            findings.append(
                _evaluation_finding(source, normalized, line_number, "invalid_json")
            )
            continue
        if not isinstance(payload, dict):
            findings.append(
                _evaluation_finding(source, normalized, line_number, "invalid_object")
            )
            continue
        for field in sorted(_nested_forbidden_evaluation_fields(payload)):
            findings.append(
                _evaluation_finding(source, normalized, line_number, field)
            )
    return findings


def _is_protected_evaluation_jsonl(path: str) -> bool:
    folded = path.replace("\\", "/").casefold()
    return folded.startswith("evals/") and folded.endswith(".jsonl")


def _nested_forbidden_evaluation_fields(value: object) -> set[str]:
    fields: set[str] = set()
    if isinstance(value, dict):
        fields.update(_FORBIDDEN_EVALUATION_FIELDS.intersection(value))
        for child in value.values():
            fields.update(_nested_forbidden_evaluation_fields(child))
    elif isinstance(value, list):
        for child in value:
            fields.update(_nested_forbidden_evaluation_fields(child))
    return fields


def _valid_development_gold(text: str) -> bool:
    observed: dict[str, tuple[str, ...]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict) or set(payload) != {
            "case_id",
            "gold_files",
            "schema_version",
        }:
            return False
        if payload.get("schema_version") != "reposcope.eval.gold.v1":
            return False
        case_id = payload.get("case_id")
        gold_files = payload.get("gold_files")
        if (
            not isinstance(case_id, str)
            or case_id in observed
            or not isinstance(gold_files, list)
            or not all(isinstance(path, str) for path in gold_files)
        ):
            return False
        observed[case_id] = tuple(gold_files)
    return observed == _PUBLIC_DEVELOPMENT_GOLD


def _evaluation_finding(
    source: str, path: str, line: int, field: str
) -> SecretFinding:
    rule = f"forbidden_evaluation_field_{field}"
    return SecretFinding(
        source=source,
        path=path,
        line=line,
        rule=rule,
        sha256=hashlib.sha256(f"{path}:{line}:{rule}".encode("utf-8")).hexdigest(),
    )
