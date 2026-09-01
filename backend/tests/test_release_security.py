from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import app.release_security as release_security
from app.release_security import (
    SecretAllowlist,
    forbidden_tracked_paths,
    scan_bytes,
    scan_git_history,
    tracked_paths,
)


def test_secret_scan_reports_location_and_rule_without_leaking_value() -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"

    findings = scan_bytes("artifact", "dist/app.js", f'const token = "{secret}";'.encode())

    assert [(item.source, item.path, item.line, item.rule) for item in findings] == [
        ("artifact", "dist/app.js", 1, "openai_api_key"),
    ]
    assert secret not in repr(findings)


def test_secret_scan_allows_only_the_exact_reviewed_digest(tmp_path: Path) -> None:
    allowed = "AKIAABCDEFGHIJKLMNOP"
    allowlist_path = tmp_path / "allowlist.json"
    allowlist_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "path": "tests/fixture.py",
                        "rule": "aws_access_key",
                        "sha256": "457643f44d19aed85fd756aa50cc0cd6b57376d4e8f5a72f9f85972a522002a3",
                        "reason": "synthetic redaction fixture",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    allowlist = SecretAllowlist.load(allowlist_path)

    assert scan_bytes("tracked", "tests/fixture.py", allowed.encode(), allowlist) == []
    assert scan_bytes("tracked", "src/config.py", allowed.encode(), allowlist)


def test_git_history_scan_finds_a_removed_secret(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "RepoScope Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "config.txt"
    tracked.write_text("github_pat_abcdefghijklmnopqrstuvwxyz0123456789", encoding="utf-8")
    subprocess.run(["git", "add", "config.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    tracked.write_text("clean", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "remove fixture"], cwd=tmp_path, check=True)

    findings = scan_git_history(tmp_path)

    assert any(item.path == "config.txt" and item.rule == "github_token" for item in findings)


def test_git_history_scan_rejects_removed_private_curation_paths(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "RepoScope Test"], cwd=tmp_path, check=True)
    evidence = tmp_path / "local" / "evaluation" / "curation" / "case" / "evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"gold_files":["src/parser.py"]}', encoding="utf-8")
    subprocess.run(["git", "add", "-f", "local/evaluation/curation/case/evidence.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "leak fixture"], cwd=tmp_path, check=True)
    evidence.unlink()
    subprocess.run(["git", "commit", "-qam", "remove fixture"], cwd=tmp_path, check=True)

    findings = scan_git_history(tmp_path)

    assert any(
        item.path == "local/evaluation/curation/case/evidence.json"
        and item.rule == "forbidden_history_path"
        for item in findings
    )


def test_git_history_allowlist_is_scoped_to_every_path_for_shared_blob(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "RepoScope Test"], cwd=tmp_path, check=True)
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "a_allowed.txt").write_text(secret, encoding="utf-8")
    (tmp_path / "z_unreviewed.txt").write_text(secret, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "shared fixture"], cwd=tmp_path, check=True)
    allowlist = SecretAllowlist(
        frozenset(
            {
                (
                    "a_allowed.txt",
                    "aws_access_key",
                    hashlib.sha256(secret.encode()).hexdigest(),
                )
            }
        )
    )

    findings = scan_git_history(tmp_path, allowlist)

    assert any(
        item.path == "z_unreviewed.txt" and item.rule == "aws_access_key"
        for item in findings
    )


def test_secret_scan_ignores_binary_content() -> None:
    assert scan_bytes("artifact", "asset.bin", b"\x00sk-proj-abcdefghijklmnopqrstuvwxyz0123456789") == []


def test_secret_scan_fails_closed_when_a_file_exceeds_its_scan_limit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_security, "_MAX_SCAN_BYTES", 8)

    findings = scan_bytes("artifact", "dist/large.js", b"no-secret-here")

    assert len(findings) == 1
    assert findings[0].rule == "scan_limit_exceeded"


def test_compose_frontend_healthcheck_uses_explicit_ipv4_loopback() -> None:
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text(encoding="utf-8")

    assert "wget -q -O /dev/null http://127.0.0.1/" in compose


def test_private_curation_evidence_is_ignored_and_forbidden_if_tracked() -> None:
    root = Path(__file__).parents[2]
    private_path = "local/evaluation/curation/example/evidence.v1.json"

    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", private_path], cwd=root
    )

    assert ignored.returncode == 0
    assert forbidden_tracked_paths([private_path]) == [private_path]
    assert forbidden_tracked_paths(tracked_paths(root)) == []
