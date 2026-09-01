from __future__ import annotations

import argparse
from pathlib import Path

from app.release_security import (
    SecretAllowlist,
    forbidden_tracked_paths,
    scan_files,
    scan_git_history,
    tracked_paths,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan RepoScope Git history and release artifacts without printing secret values.")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument("--allowlist", type=Path, required=True)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    allowlist = SecretAllowlist.load(args.allowlist.resolve())
    paths = tracked_paths(root)
    findings = scan_git_history(root, allowlist)
    findings.extend(scan_files(root, (root / path for path in paths), "tracked", allowlist))
    for artifact in args.artifact:
        artifact_root = artifact.resolve()
        if artifact_root.exists():
            findings.extend(scan_files(root, artifact_root.rglob("*"), "artifact", allowlist))
    forbidden = forbidden_tracked_paths(paths)
    for finding in findings:
        print(f"{finding.source}:{finding.path}:{finding.line}: {finding.rule} ({finding.sha256[:12]})")
    for path in forbidden:
        print(f"tracked-artifact:{path}")
    if findings or forbidden:
        print(f"release security scan failed: {len(findings)} secret finding(s), {len(forbidden)} forbidden tracked path(s)")
        return 1
    print("release security scan passed: Git history and artifacts contain no unreviewed high-confidence secrets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
