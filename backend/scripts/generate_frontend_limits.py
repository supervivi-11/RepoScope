from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_TARGET = REPO_ROOT / "frontend" / "src" / "generated" / "limits.ts"
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.report_limits import FRONTEND_LIMITS  # noqa: E402


def render_limits() -> str:
    lines = [
        "// Generated from backend/app/report_limits.py; do not edit by hand.",
        "",
    ]
    for name, value in FRONTEND_LIMITS.items():
        lines.append(f"export const {name} = {value} as const;")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in TypeScript limits are out of date",
    )
    args = parser.parse_args()

    expected = render_limits()
    if args.check:
        actual = FRONTEND_TARGET.read_text(encoding="utf-8")
        if actual != expected:
            sys.stderr.write(
                f"{FRONTEND_TARGET} is out of date; run this generator without --check.\n"
            )
            return 1
        return 0

    FRONTEND_TARGET.parent.mkdir(parents=True, exist_ok=True)
    FRONTEND_TARGET.write_text(expected, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
