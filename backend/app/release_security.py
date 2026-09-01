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
        name = path.rsplit("/", 1)[-1].lower()
        if name == ".env" or name.endswith((".sqlite", ".sqlite3", ".db")):
            forbidden.append(path)
        elif path.startswith(("local/", "dist/", "frontend/dist/", "test-results/", "frontend/test-results/", "playwright-report/", "frontend/playwright-report/")):
            forbidden.append(path)
        elif path.startswith("evals/gold/") or path.startswith("evals/local-results/"):
            forbidden.append(path)
    return sorted(set(forbidden))
