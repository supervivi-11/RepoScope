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
