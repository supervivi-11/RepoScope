## Task 6 report: React investigation UI and static demo mode

### Outcome

Implemented the Chinese-first RepoScope investigation workspace, the typed live API/SSE client, truthful branch-aware status and feedback state, and exactly three bundled deterministic placeholder demonstrations. The default production build is explicitly static/demo-only; local development, Compose, and the Playwright preview use the explicit `live` build mode.

Delivery commit: `feat: add investigation workspace and demos` (this report is included in that commit).

### Review-fix round — live runtime hardening

Took over the interrupted production-runtime review-fix diff without reverting prior worker changes. The inherited implementation adds the backend runtime composition root, worker service/CLI, schema-aware readiness probe, snapshot janitor, atomic review publishing, blocking-work offload, Compose migration/API/worker split, Alembic image assets, frontend live dev proxy, GET-authoritative SSE behavior, static/live build isolation, route-key normalization, and bounded public JSON/event handling.

Additional TDD fix in this takeover:

- RED: `npm test -- --reporter=dot src/App.test.tsx` failed with the new neutral letter-spacing regression, showing `-0.025em`, `0.08em`, `0.14em`, and `0.06em`.
- GREEN: set the four interface `letter-spacing` declarations to `0`; the focused App suite then reported 10 passed.

Fresh focused baseline for the inherited runtime diff:

- `.\.venv\Scripts\python.exe -m pytest -W error -q backend\tests\test_runtime_service.py backend\tests\test_analysis_persistence.py backend\tests\test_agent_tool_dispatch.py backend\tests\test_health.py backend\tests\test_analysis_worker_leases.py backend\tests\test_analysis_worker_security.py backend\tests\test_ingestion_service.py backend\tests\test_investigation_tools.py`: 54 passed.
- `npm test -- --reporter=dot src/api/sse.test.ts src/views/LiveWorkspace.test.tsx`: 14 passed.

Fresh review-fix verification:

- `npm test -- --reporter=dot`: 60 passed, 11 files.
- `npm run typecheck`: PASS.
- `npm run test:build-config`: PASS, `build configuration isolation verified`.
- `npm run build`: PASS, production static build, 77 modules, JS 274.24 kB / 84.06 kB gzip, CSS 18.25 kB / 4.74 kB gzip.
- `npm run build:live`: PASS, explicit live build, 77 modules, JS 274.24 kB / 84.06 kB gzip, CSS 18.25 kB / 4.74 kB gzip.
- Production bundle scan: PASS for local absolute paths, unexpanded Vite variables, and known test secret markers.
- `npx playwright test --list`: PASS, four Chromium specs listed.
- `npx playwright test`: NOT VERIFIED because Chromium is not installed locally; every spec failed at browser launch with missing `C:\Users\1\AppData\Local\ms-playwright\chromium_headless_shell-1234\chrome-headless-shell-win64\chrome-headless-shell.exe`. The wrapper was interrupted after recording the launch failures because teardown did not exit promptly.
- `.\.venv\Scripts\python.exe -m pytest -W error -q backend\tests`: 214 passed.
- `.\.venv\Scripts\python.exe -m compileall -q backend\app backend\tests`: PASS.
- `.\.venv\Scripts\python.exe -m pip check`: PASS, no broken requirements.
- `..\.venv\Scripts\python.exe -m alembic upgrade head --sql` from `backend`: PASS, generated PostgreSQL DDL ending at `20260830_0001`.
- `docker compose config --quiet` with isolated `DOCKER_CONFIG`: PASS.
- `docker version --format '{{.Server.Version}}'` with isolated `DOCKER_CONFIG`: NOT VERIFIED because the daemon pipe `//./pipe/docker_engine` does not exist.

Generated verification artifacts `frontend/dist`, `frontend/test-results`, and `frontend/playwright-report` were removed after the scan. `node_modules` remains ignored and was not modified as a deliverable.

### Review-fix round 2 — runtime supervision and cleanup hardening

Addressed the concrete rereview findings on top of clean `1b2ab47` without subagents or external live calls.

Implemented fixes:

- Normalized blank `REPOSCOPE_OPENAI_BASE_URL` to `None` at the OpenAI-compatible model settings boundary so the Compose default uses the provider default; nonblank non-HTTP(S) values still fail validation without logging secrets.
- Added a shared snapshot root validator used by settings, janitor, ingestion materialization, and snapshot cleaner. It rejects empty/current paths, filesystem/drive roots, paths directly under a filesystem root, user home, detected repo/workspace roots, symlink roots, and non-dedicated leaves; janitor deletion still only removes expired inactive direct directories whose resolved paths remain under the configured snapshot root.
- Reworked `worker_main` into a supervisor: graceful signal returns exit code 0 after both managed tasks stop and resources close once; unexpected worker/janitor error or cancellation stops the peer, awaits cleanup, closes once, and returns exit code 1 for Compose restart. Logs include only the failed component name.
- Replaced the generic 1 MiB API JSON response limit with endpoint-specific caps. `getAnalysis` now uses a finite limit derived from the decoder’s maximum current report plus two report-history versions and analysis metadata; small create/feedback/error responses keep a small 64 KiB cap. SSE remains bounded separately in `sse.ts`.
- Replaced the low-contrast placeholder color `#5d6c65` with `#8da098` and added deterministic WCAG-AA contrast coverage for normal-size muted/faint/error/placeholder text pairs.

Round-2 RED evidence:

- `.\.venv\Scripts\python.exe -m pytest -q backend\tests\test_runtime_service.py`: 11 failed, 4 passed, 1 skipped. Failures matched blank OpenAI base validation, missing worker-main supervision injection/exit behavior, and broad snapshot roots being accepted.
- `npm test -- --run src/api/client.test.ts src/App.test.tsx`: 2 failed, 22 passed. Failures matched the old 1 MiB analysis response cap and placeholder contrast ratio 3.43:1.

Round-2 GREEN/focused verification:

- `.\.venv\Scripts\python.exe -m pytest -q backend\tests\test_runtime_service.py`: 15 passed, 1 skipped. The skip is the symlink-root regression when this Windows environment cannot create a test symlink.
- `.\.venv\Scripts\python.exe -m pytest -q backend\tests\test_runtime_service.py backend\tests\test_ingestion_service.py`: 23 passed, 1 skipped.
- Reviewer-original focused backend diagnostics: `.\.venv\Scripts\python.exe -m pytest -q backend\tests\test_runtime_service.py backend\tests\test_analysis_persistence.py backend\tests\test_analysis_queue_checkpoints.py backend\tests\test_agent_tool_dispatch.py backend\tests\test_health.py backend\tests\test_analysis_worker_leases.py backend\tests\test_analysis_worker_security.py backend\tests\test_ingestion_service.py backend\tests\test_investigation_tools.py backend\tests\test_safe_archive.py`: 93 passed, 1 skipped.
- Reviewer-original focused frontend diagnostics: `npm test -- --run src/api/client.test.ts src/api/sse.test.ts src/views/LiveWorkspace.test.tsx src/router.test.ts src/components/ReportView.test.tsx src/components/FeedbackControls.test.tsx src/App.test.tsx`: 45 passed, 7 files.

Round-2 full verification:

- `npm test`: 61 passed, 11 files.
- `npm run typecheck`: PASS; `tsconfig.json` includes `src`, `e2e`, `vite.config.ts`, and `playwright.config.ts`.
- `npm run test:build-config`: PASS, `build configuration isolation verified`.
- `npm run build`: PASS, production static build, 77 modules, JS 274.27 kB / 84.08 kB gzip, CSS 18.25 kB / 4.74 kB gzip.
- Static bundle scan: PASS for OpenAI/GitHub/token sentinel strings and build-config secret marker.
- `npm run build:live`: PASS, explicit live build, 77 modules, JS 274.27 kB / 84.08 kB gzip, CSS 18.25 kB / 4.74 kB gzip.
- Live bundle scan: PASS for OpenAI/GitHub/token sentinel strings and build-config secret marker.
- `npx playwright test --list`: PASS, four Chromium specs listed.
- `.\.venv\Scripts\python.exe -m pytest -W error -q`: 226 passed, 1 skipped.
- `.\.venv\Scripts\python.exe -m compileall -q backend`: PASS.
- `.\.venv\Scripts\python.exe -m pip check`: PASS, no broken requirements.
- `..\.venv\Scripts\alembic.exe upgrade head --sql` from `backend`: PASS, generated offline PostgreSQL DDL ending at `20260830_0001`.
- `docker compose config --quiet` with isolated `DOCKER_CONFIG`: PASS.

Limitations still not claimed as verified: no real GitHub/model/OpenAI calls, no live PostgreSQL/Docker daemon startup, and no Chromium browser execution beyond Playwright test discovery.

### TDD evidence

- Baseline RED: `npm test -- --reporter=dot` collected `e2e/workspace.spec.ts` as Vitest and failed while the existing 26 unit tests passed.
- Audit RED: after writing regression tests for the reasoner findings, the suite reported 14 failures / 27 passes. The failures covered skip-link route corruption, unsafe static live routing, idempotency-key churn, unsafe API bases, response identity mismatch, unbounded/unabortable SSE, secret-bearing public events, and fabricated linear status history.
- Feedback lifecycle RED: the focused `LiveWorkspace` test demonstrated that a backend `REVIEW_READY` feedback acknowledgement re-enabled duplicate commands after query refresh.
- Public-text RED: contract decoding preserved `Authorization: Bearer sk-live-super-secret` and credential-bearing URLs.
- Configuration RED: unsafe `VITE_API_BASE_URL` threw during React render rather than producing a fixed safe surface.
- Active-reader cleanup RED: unmount initially did not cancel the already acquired stream reader; the stream fixture was refined to witness the reader lifecycle, then the abort propagation was verified.
- GREEN: each regression was implemented at the narrowest production boundary and rerun focused before the full suite.

### Implemented surfaces

- Hash-restorable landing, live analysis, demo, and safe not-found/static-disabled routes.
- Skip link focuses `main` without replacing the application hash route.
- Explicit `VITE_REPOSCOPE_MODE`: unknown/absent defaults to `static`; `.env.live`, local development, Compose, and built-preview E2E explicitly enable `live`.
- Static mode blocks create/get/SSE paths even when an API base and fetch implementation are supplied.
- Strict API base validation permits blank/same-origin relative paths and credential-free HTTP(S) origin paths, while rejecting credentials, query, fragment, protocol-relative, non-HTTP, whitespace/control-bearing configuration with a fixed non-reflective error.
- Runtime decoding for Task 5 analysis/report/history/feedback/event/demo contracts, including requested/returned analysis identity equality.
- Central public-text credential redaction plus canonical event error and unknown-event labels before DOM rendering.
- Fetch-stream SSE with named event parsing, monotonic IDs, split-chunk support, duplicate/out-of-order suppression, explicit `Last-Event-ID`, bounded 64 KiB frames/buffer, bounded reconnect backoff, polling fallback, terminal close, and route/unmount reader cancellation.
- Truthful status presentation uses only current and observed event states. Direct acceptance never implies `REVISING`; failure never marks unobserved successful stages complete.
- Workspace-owned accept/revise command lock persists across `REVIEW_READY` query refreshes and prevents duplicates until a transition/terminal result or safe conflict/error is observed.
- One idempotency key is retained for a submission intent and retry, then rotated when input changes or creation succeeds.
- Report/evidence/empty/insufficient/failure/reconnecting surfaces; validated immutable GitHub permalinks; accessible copy and feedback controls.
- Exactly three bundled, versioned, deterministic schema/product walkthroughs with play/pause/restart/step and visible `预生成演示` / non-benchmark labeling.
- Responsive rules for mobile/tablet/desktop, visible keyboard focus, semantic headings/landmarks, textual status meanings, local-only assets, and `prefers-reduced-motion` handling.
- Nginx same-origin API proxy and Compose live-build configuration.

### Verification

- `npm test -- --reporter=dot`: **48 passed**, 9 files; Vitest excludes `e2e/**`.
- `npm run typecheck`: **PASS**; includes `src`, `e2e`, `vite.config.ts`, and `playwright.config.ts`.
- `npm run build`: **PASS**; production static bundle 77 modules, JS 273.82 kB (83.98 kB gzip), CSS 18.26 kB (4.76 kB gzip).
- `npm run build:live`: **PASS** during live preview configuration verification.
- production bundle scan: **PASS** for local absolute paths, unexpanded Vite variables, and known test secrets.
- `npx playwright test --list`: **PASS**, four Chromium tests listed (demo zero-network playback, live 202/reconnect/review/accept lifecycle, refreshed hash route, 360 px keyboard/overflow smoke).
- `docker compose config --quiet`: **PASS** with an isolated empty Docker config directory.
- `..\.venv\Scripts\python.exe -m pytest -W error -q backend\tests` from repository root: **207 passed** in 6.11 s.
- `git diff --check`: **PASS**.
- Generated `dist`, `.tmp-npm-cache`, `playwright-report`, and `test-results` were removed; `node_modules` remains ignored and untracked.

### Browser limitation

The browser specifications and configuration are complete and enumerate successfully, but this machine has no Playwright Chromium binary. `npx playwright test` failed before test execution with:

`Executable doesn't exist at C:\Users\1\AppData\Local\ms-playwright\chromium_headless_shell-1234\chrome-headless-shell-win64\chrome-headless-shell.exe`

Deferred Task 8 commands, after external browser installation is permitted:

1. `cd frontend`
2. `npx playwright install chromium`
3. `npx playwright test`

No browser download was attempted because Task 6 explicitly permits recording this external limitation.

### Changed areas

- Frontend contracts/configuration: `src/contracts.ts`, `src/config.ts`, client/SSE modules, Vite/TypeScript/Playwright configuration.
- UI: focused views/components/hooks/router, responsive styles, index metadata.
- Demo: three bundled v1 artifacts and deterministic player.
- Tests: 48 Vitest cases and four built-preview Playwright cases.
- Runtime packaging: frontend Dockerfile, Nginx config, Compose live build args, package manifest/lock.

### Remaining concerns handed forward

- Actual Chromium rendering, keyboard, mobile overflow, request interception, and live mocked browser flow remain unverified until the binary is installed.
- Bundled cases intentionally remain `example/*` schema/product walkthrough placeholders and must be replaced by three truthful real historical exports in Task 7.
- No public deployment, real GitHub/model request, live PostgreSQL service, or external asset was contacted in this task.

### Review-fix round 3 — streaming evidence and bounded report contract

Completed the interrupted final Task 6 hardening diff on top of `6cd0a3d`.

Implemented:

- Changed the worker from final-only `ainvoke` execution to LangGraph `astream(..., stream_mode="values", durability="sync")`, publishing non-boundary investigation events as soon as their checkpointed state is visible while retaining atomic report/status publication at `REVIEW_READY` and terminal boundaries.
- Backfilled checkpointed events after recovery, enforced contiguous append-only graph event sequences, fenced stale attempts through the existing lease contract, and rejected a reused graph dedupe key when its type or sanitized payload diverges.
- Replaced nested full citations in hypotheses with compact immutable location references resolved only against deterministically validated top-level evidence. Unresolved alternatives are removed and a primary hypothesis without validated evidence still downgrades to `insufficient_evidence`.
- Added one Python source of truth for report/response limits, a checked-in generated TypeScript module, a 1 MiB serialized-report cap, a 3 MiB analysis-response cap, and at most one previous report in `report_history`; the latest report appears only as `current_report`.
- Added line-preserving and idempotent credential redaction for LF, CRLF, and CR content, including cross-line authorization values, assignments, provider tokens, credential URLs, and private-key blocks. Reports are sanitized again at the persistence boundary after citation validation.
- Made `attempts_exhausted` a canonical shared failure so storage, the failure event, and the GET API preserve the safe retry-budget result.
- Updated worker fakes to the public streaming interface and retained the legacy/foreign-checkpoint recovery rule: any existing checkpoint resumes with `None` input even when its channel values cannot be parsed as `AnalysisState` for event backfill.

Round-3 RED evidence:

- Focused backend contract suite initially reported 5 failures: three stale full-citation fixtures, an unresolved alternative hypothesis, and duplicated latest report history.
- Focused frontend contract suite initially reported 3 failures: old full-citation hypothesis decoding and the obsolete derived response-size cap.
- New LF/CRLF/CR redaction regressions failed because the authorization expression consumed one original line delimiter.
- The real repository/API retry-budget regression failed because `attempts_exhausted` was rewritten to `internal_error`.
- Full backend regression exposed old `ainvoke` test doubles and a checkpoint-resume regression; both were updated/fixed against the streaming contract.

Round-3 verification:

- Focused report/stream/API backend suite: **50 passed**.
- Focused worker lease/security/streaming suite: **21 passed**.
- Full backend: `242 passed, 1 skipped` with warnings treated as errors. The existing skip is the Windows symlink-root case when symlink creation is unavailable.
- Full frontend: `65 passed`, 11 files.
- `npm run typecheck`: **PASS**.
- `npm run test:build-config`: **PASS** (`build configuration isolation verified`).
- `npm run build`: **PASS**, 78 modules, JS 274.97 kB / 84.34 kB gzip, CSS 18.25 kB / 4.74 kB gzip.
- `npm run build:live`: **PASS**, 78 modules, JS 274.97 kB / 84.34 kB gzip, CSS 18.25 kB / 4.74 kB gzip.
- `npx playwright test --list`: **PASS**, four Chromium scenarios listed. Browser execution remains subject to the previously recorded missing local Chromium binary.
- `backend/scripts/generate_frontend_limits.py --check`: **PASS**.
- Python `compileall`, `pip check`, and `git diff --check`: **PASS**.

Final reviewer findings and resolution:

- Fixed incomplete public-text redaction for quoted object keys, prefixed environment variables, AWS access-key IDs, and quoted values containing spaces. A shared backend/frontend corpus verifies the safe output, delimiter preservation, idempotence, and the `token_count` / `AuthorizationPolicy` false-positive boundaries.
- Added a legacy persistence compatibility reader for pre-compact-reference report rows and LangGraph checkpoints. It recursively converts nested serializer-restored `BaseModel` values to plain data before strict validation, upgrades full hypothesis citations to summaries, and safely downgrades legacy reports that exceed the current bounded contract.
- Added a real `build_checkpoint_serializer()` round-trip regression using the actual `channel_values` shape: a mapping containing an oversized `AnalysisReport.model_construct` value can no longer bypass validation.
- Final focused rereview: both Important findings fixed; no new Critical or Important findings; reviewer verdict **Ready**.
