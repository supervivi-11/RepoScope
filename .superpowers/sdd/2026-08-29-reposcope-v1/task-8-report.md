# Task 8 report — release candidate verification

## Scope

Task 8 started from `a8cc704` on `codex/reposcope-v1`. It ran final backend, frontend, browser, security, migration and Docker checks; fixed only failures exposed by those checks; prepared but did not publish `v0.1.0`.

No real model, paid API, real benchmark, push, deployment, tag or GitHub Release was used.

## Implemented

### Browser and build verification

- Repaired two invalid/flaky Playwright assertions: a strict-mode ambiguous text locator and an SSE reconnect race.
- Corrected the E2E report fixture so hypothesis evidence follows the summary-only public contract.
- Split browser verification into explicit `@live` and `@static` modes.
- Added a finite Node static server and lifecycle-owning E2E runner. This prevents Windows Playwright from hanging while terminating a managed preview subprocess.
- The static Chromium suite now proves the production static bundle plays bundled demos without API or GitHub requests.

### Release security scan

- Added a dependency-free scanner for current tracked files, all Git-history blobs and selected release artifact directories.
- Added high-confidence rules for private keys, OpenAI-style keys, GitHub tokens and AWS access keys.
- Findings never print the matched value; only source, normalized path, line, rule and digest prefix are exposed.
- Synthetic security-test credentials require an exact path/rule/SHA-256 allowlist entry with a reason.
- Files and history blobs above the 5 MiB scan limit fail closed with `scan_limit_exceeded` instead of being skipped.
- The same command rejects tracked `.env`, database, build/test output, hidden gold and local-result paths.

### Empty-configuration Compose behavior

- Fixed the frontend healthcheck to use `127.0.0.1`; Alpine resolved `localhost` to IPv6 while nginx listened on IPv4, producing a false unhealthy state.
- Made empty or whitespace model credentials a supported safe state.
- Added `DisabledWorkerService`: without a Key it logs a fixed `model_not_configured` code, does not build `ChatOpenAI`, does not claim jobs, keeps janitor/runtime lifecycle active and exits cooperatively.
- With a real nonblank Key the existing active worker path remains unchanged; malformed nonblank base URLs still fail closed.

### Release artifacts

- Added `docs/release-verification.md` with exact commands, outputs, environment notes and explicit non-results.
- Added the unpublished `docs/releases/v0.1.0.md` Release draft.
- Updated README, architecture and release checklist to describe actual verified behavior and remaining gaps.

## Verification evidence

- Backend: `276 passed, 1 skipped` with `-W error`; compileall and pip check passed.
- Frontend: 11 Vitest files / 65 tests; typecheck and build-config isolation passed.
- Playwright Chromium: live 1 passed; static 3 passed.
- Static and live Vite builds passed; both transformed 78 modules.
- Frontend limit generator, evaluation Schema check and 12-slot 6/6 catalog validation passed.
- Release scan passed for Git history, current tracked files and `frontend/dist`.
- Fresh named Docker volumes migrated PostgreSQL to `20260830_0001`; all four analysis tables exist; pgvector 0.8.6 is available.
- API `/health` and `/ready` passed; frontend and PostgreSQL were healthy; migrate exited 0.
- Empty-Key worker remained running with restart count 0, queue count 0 and fixed degraded log; no model endpoint was configured or called.
- The isolated `reposcope_task8` containers, network and volumes were removed after verification.

## Environment incident handled truthfully

The local Docker daemon had many third-party Docker Hub mirrors. Standard `pgvector/pgvector:pg16` pull failed twice after layer download because a mirror returned HTML for an OCI referrers request. The same image was pulled explicitly through a configured mirror, and its returned index digest was checked against Docker Hub before applying the local standard tag. Repository Compose references were not changed. This workaround and digest are recorded in `docs/release-verification.md`.

## Not executed / not claimed

- The 12 historical cases remain unfilled metadata slots; no real evaluation score exists.
- Public demos remain clearly labelled product-flow placeholders, not historical analysis exports.
- GIF/video files were not recorded; only the reviewed recording guide exists.
- GitHub advisory availability was not tested against a public repository URL.
- No push, deploy, tag or Release publication occurred.

## Review and handoff

The final independent reviewer found no Critical or Important issues. Its only residual risks were the intentionally text-focused scanner skipping NUL-containing binary files and symlinks, and the E2E runner lacking an automated external-interruption cleanup test; neither affects the verified tracked tree or built artifacts. Focused reviewer verification passed with `23 passed, 1 skipped`.

The final full backend verification passed with `276 passed, 1 skipped`; frontend unit, type, build-isolation and live/static Chromium results remain green. The intended Conventional Commit is `chore: verify reposcope release candidate`.
