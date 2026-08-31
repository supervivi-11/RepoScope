from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.report_limits import (
    ANALYSIS_RESPONSE_MAX_BYTES,
    PREVIOUS_REPORT_HISTORY_MAX,
    REPORT_SERIALIZED_MAX_BYTES,
)


def test_frontend_limits_are_generated_from_backend_source_truth() -> None:
    """Breaks if TypeScript response caps drift from backend report contracts."""
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "backend/scripts/generate_frontend_limits.py",
            "--check",
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_authoritative_response_caps_are_finite_and_documented() -> None:
    assert REPORT_SERIALIZED_MAX_BYTES == 1024 * 1024
    assert PREVIOUS_REPORT_HISTORY_MAX == 1
    assert ANALYSIS_RESPONSE_MAX_BYTES == 3 * 1024 * 1024
