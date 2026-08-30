# Task 5 report — Persistent analyses API and SSE

## Status

DONE

## RED/GREEN evidence

### Cycle 1 — durable schema, repository, events, reports, and feedback

- RED: `./.venv/Scripts/python.exe -m pytest
  backend/tests/test_analysis_persistence.py backend/tests/test_migrations.py -q`
  failed during collection with `ModuleNotFoundError: No module named
  'app.analysis'`. No persistent status/domain, SQLAlchemy tables, repository,
  or Task 5 migration existed.
- GREEN: async SQLite exercised the real SQLAlchemy repository while migration
  assertions compiled PostgreSQL types and offline SQL. Idempotent create/key
  conflict, atomic monotonic event allocation, original/revised report history,
  feedback idempotency/state guards, exact statuses, UUID/timezone/JSONB shape,
  and all four tables produced `9 passed in 1.55s`.

### Cycle 2 — PostgreSQL lease queue and checkpoint safety

- RED: the focused queue/checkpoint file failed collection with
  `ModuleNotFoundError: No module named 'app.analysis.checkpoints'`.
- GREEN: the production claim selector compiles to `SELECT ... FOR UPDATE SKIP
  LOCKED`, excludes live leases, deterministically reclaims expired queued or
  active jobs, increments attempts, and enforces worker ownership for
  heartbeat/release/terminal operations. `AsyncPostgresSaver` configuration
  uses a per-analysis `thread_id`, explicit Task 4 constructor allowlist, and
  `pickle_fallback=False`. The focused selection produced `5 passed in 1.37s`.

### Cycle 3 — worker orchestration and safe failures

- RED: the worker/security file failed collection because the failure mapping,
  recursive redaction, and worker boundary did not exist.
- GREEN: injected ingestion/index/tools/graph/checkpointer/cleanup boundaries
  persist status/events/report/state and make completed delivery idempotent;
  typed failures store only stable codes and canonical messages. The initial
  focused selection produced `3 passed in 1.42s`.
- Revision recovery RED: the immutable-snapshot regression failed because a
  revision called ingestion a second time (`2 != 1`) and started from a fresh
  graph input.
- Revision recovery GREEN: revision now restores persisted snapshot/state,
  rebuilds static tools, and sends `Command(resume={...})` through the same
  analysis checkpoint thread. The focused regression produced `1 passed in
  1.42s`.
- Safe-failure RED: direct misuse of `fail_safe()` persisted
  `invented_code` and `Authorization sk-unsafe-secret`.
- Safe-failure GREEN: persistence accepts only the exact canonical public
  failure triples and otherwise stores `internal_error` / `Analysis failed
  safely.`. The focused regression produced `1 passed in 1.37s`.

### Cycle 4 — FastAPI/OpenAPI, feedback, demos, and SSE

- RED: the endpoint selection failed collection with `ModuleNotFoundError: No
  module named 'app.api'`.
- GREEN: the API returns 202 for a new analysis and 200 for idempotent replay,
  validates GitHub-only inputs and bounded headers/bodies, exposes current and
  historical reports, maps not-found/conflict/internal errors safely, keeps
  `/health` exact, adds separate `/ready`, serves three committed/versioned
  static artifacts, and streams replayable named SSE events with numeric IDs,
  heartbeat, terminal close, and disconnect cancellation. The focused
  selection produced `8 passed in 1.73s`.

## Verification

- Final Task 5 selection with warnings promoted to errors:
  `./.venv/Scripts/python.exe -m pytest
  backend/tests/test_analysis_persistence.py
  backend/tests/test_analysis_queue_checkpoints.py
  backend/tests/test_analysis_worker_security.py
  backend/tests/test_analysis_api.py backend/tests/test_migrations.py -q -W
  error` → `27 passed in 2.43s`.
- Final full backend suite:
  `./.venv/Scripts/python.exe -m pytest backend -q` → `177 passed in 3.58s`.
- Compile check:
  `./.venv/Scripts/python.exe -m compileall -q backend/app backend/tests
  backend/migrations` → PASS (exit 0, no output).
- Dependency compatibility:
  `./.venv/Scripts/python.exe -m pip check` → `No broken requirements found.`
- Dependency resolution installed `aiosqlite 0.22.1`,
  `langgraph-checkpoint-postgres 3.1.2`, and `psycopg-pool 3.3.1`; PostgreSQL
  checkpoint 3.1.2 resolved against the existing `langgraph-checkpoint 4.2.0`.
- PostgreSQL-dialect offline migration:
  `DATABASE_URL=postgresql+psycopg://... python -m alembic -c
  backend/alembic.ini upgrade head --sql` → PASS. Output contains all four
  `CREATE TABLE` statements, PostgreSQL `UUID`, `JSONB`, timezone-aware
  timestamps, the exact eight-value status check, unique idempotency key,
  unique event sequence, report version, and feedback fingerprint constraints.
- `git diff --cached --check` → PASS before the feature commit.
- Staged path audit → PASS: no `.idea` path was staged; `backend/.idea/`
  remains ignored and no IDE file is tracked.

## Live PostgreSQL / Docker status

NOT VERIFIED for Task 5.

`docker version --format '{{.Server.Version}}'` failed because
`//./pipe/docker_engine` does not exist. The Docker daemon was unavailable, so
no live PostgreSQL migration, concurrent `SKIP LOCKED` claim, or live
`AsyncPostgresSaver` setup was run. PostgreSQL behavior was verified only at
the SQL compilation/configuration boundary plus async repository behavior on
SQLite. Task 8 must run the live Compose migration/claim/checkpoint checks.

## Files changed

- `.gitignore`
- `backend/pyproject.toml`
- `backend/app/db.py`
- `backend/app/main.py`
- `backend/app/api.py`
- `backend/app/demo_cases.json`
- `backend/app/analysis/__init__.py`
- `backend/app/analysis/domain.py`
- `backend/app/analysis/models.py`
- `backend/app/analysis/repository.py`
- `backend/app/analysis/queue.py`
- `backend/app/analysis/checkpoints.py`
- `backend/app/analysis/failures.py`
- `backend/app/analysis/worker.py`
- `backend/migrations/env.py`
- `backend/migrations/versions/20260830_0001_persistent_analyses.py`
- `backend/tests/test_migrations.py`
- `backend/tests/test_analysis_persistence.py`
- `backend/tests/test_analysis_queue_checkpoints.py`
- `backend/tests/test_analysis_worker_security.py`
- `backend/tests/test_analysis_api.py`

## Commit

`c8705ba4768b4cc6746b62f297178c2921d6053c` —
`feat: add persistent analysis api`

## Self-review

- The persistent enum is independent of Task 4's graph-only status enum and
  contains exactly `QUEUED`, `INGESTING`, `INDEXING`, `INVESTIGATING`,
  `REVIEW_READY`, `REVISING`, `COMPLETED`, and `FAILED`.
- UUID identity, timezone-aware timestamp columns, PostgreSQL JSONB variants,
  nullable-unique idempotency keys, append-only event identities, versioned
  reports, and durable feedback fingerprints are explicit in both ORM metadata
  and Alembic.
- Event and report sequence/version allocation uses an atomic job-row counter
  with `UPDATE ... RETURNING`; no `MAX()+1` race is used.
- PostgreSQL claiming is one transaction around `FOR UPDATE SKIP LOCKED` and a
  lease mutation. A live lease cannot be duplicated; expired work is ordered
  deterministically and its attempt count increments.
- The checkpoint serializer names every allowed Task 4 state constructor and
  rejects arbitrary pickle fallback. The SQLAlchemy driver suffix is removed
  only for the psycopg-native checkpoint connection string.
- The worker is dependency-injected and performs no repository code execution,
  subprocess, dependency installation, arbitrary network access, or snapshot
  writes. Revision restores the original immutable snapshot instead of
  resolving a new GitHub head.
- API request models use Task 2 URL/issue validators. UUID paths, header
  characters/length, request size, feedback length/nonblank rules, and the one
  revision state transition are bounded before durable mutation.
- SSE polls through short repository methods, so no session is held across a
  yield or sleep. Event names and numeric sequence IDs are stable; replay is
  strictly greater than `Last-Event-ID`; terminal jobs close after replay.
- Recursive event redaction removes authorization/token/key/password/prompt/
  raw-message fields, `sk-`/GitHub-token shapes, and URL credentials. Exception
  text never participates in public failure mapping, and persistence rechecks
  canonical errors.
- `/health` remains the exact availability contract. Database connectivity is
  only exposed through `/ready`, so a database outage does not weaken or alter
  the health response schema.

## Known limitations / concerns

- Live PostgreSQL, pgvector container startup, checkpoint table setup, and real
  multi-process worker contention remain unverified because Docker is not
  running. This is the only Task 5 verification limitation and is explicitly
  deferred to Task 8.
- Tests use injected fake GitHub/model/checkpoint boundaries and never load
  external credentials. The committed demo artifacts are static contract
  fixtures, not fabricated benchmark results.
- No Task 5 blocker requires user input.

---

## Fix round 1 — persistent workflow hardening

### Status

DONE on top of `c8705ba` + `8799d55`.

### RED/GREEN evidence

- Checkpoint setup RED: the production-factory regression observed
  `lifecycle == ["use", "reuse"]`; `AsyncPostgresSaver.setup()` was never
  called. GREEN: setup is awaited before the first yielded saver and guarded
  by a factory lock/flag so later opens reuse the idempotent database setup;
  the checkpoint file produced `5 passed` with warnings as errors.
- Lease/recovery RED: the new worker lease suite initially produced `5 failed`
  because queue calls had no attempt fence, repository mutations accepted no
  lease, active stages could not restore durable input, and the worker was not
  queue-owned. GREEN: attempt-count fencing protects heartbeat, release,
  finish, event/report/state/failure writes; a background heartbeat prevents
  reclaim during long graph calls; snapshot and issue metadata are persisted
  before indexing; `INGESTING`, `INDEXING`, and `INVESTIGATING` rebuild from
  the same snapshot; `InMemorySaver.aget_tuple()` chooses checkpoint resume
  rather than a fresh graph input; stage events and reports have durable
  idempotency keys. The combined queue/worker selection contains `19` tests,
  including `6` dedicated lease/recovery cases.
- Domain-invariant RED: runtime `action="approve"` completed an analysis and a
  malformed feedback row committed successfully. A mutation run without the
  queue transition guard also let `QUEUED -> COMPLETED` succeed. GREEN: service
  validation accepts only `accept|revise`, queue finish uses the shared legal
  transition graph, and database checks protect feedback action/comment plus
  positive/nonnegative job/event/report invariants; the focused invariant
  selection produced `3 passed`.
- Accept-flow RED: submitting `accept` immediately returned `COMPLETED` without
  invoking Task 4. GREEN: acceptance remains a durable pending command,
  becomes claimable, resumes the same checkpoint with
  `Command(resume={"action":"accept","text":null})`, then atomically stores
  the accepted graph state, `report_accepted` event, processed feedback, and
  `COMPLETED` status. Duplicate feedback/delivery creates no second event or
  report; the focused regression produced `1 passed`.
- SSE race RED: a terminal event committed between `list_events()` and
  `get_analysis()` caused `StopAsyncIteration` before the event was replayed.
  GREEN: one outer-join statement returns replay rows and job status from the
  same database snapshot; the API file produced `9 passed` at that cycle.
- Validation secrecy RED: FastAPI's default 422 body echoed the complete
  credential-bearing `repo_url`, including a short `sk-` token. GREEN: a
  dedicated `RequestValidationError` handler returns only the stable public
  code/message; the focused regression produced `1 passed`.
- Public-data safety RED: nested `raw_message`/`input_messages`, `sk-x`,
  negative counters, non-JSON progress objects, oversized events, legacy event
  JSON, and legacy error text survived validation or readback. GREEN: bounded
  recursive JSON validation and redaction run before storage and defensively
  on reads; counters are nonnegative integers; unsafe stored errors collapse to
  the canonical internal failure. The two final defense regressions produced
  `2 passed`.
- Body-limit RED: chunked and falsely-low-`Content-Length` requests reached
  Pydantic and returned 422. GREEN: middleware counts actual streamed bytes,
  retains at most the configured request bound, and returns the stable 413;
  the focused regression produced `1 passed`.
- Event-name RED: newline, carriage-return, dash, and uppercase event types all
  persisted. GREEN: storage, demo validation, and SSE formatting enforce
  `^[a-z][a-z0-9_]{0,99}$`; the parameterized regression produced `4 passed`.

### Fix-round verification

- Focused Task 5 suite with warnings promoted to errors:
  `./.venv/Scripts/python.exe -m pytest backend/tests/test_analysis_persistence.py backend/tests/test_analysis_queue_checkpoints.py backend/tests/test_analysis_worker_security.py backend/tests/test_analysis_worker_leases.py backend/tests/test_analysis_api.py backend/tests/test_migrations.py -q -W error`
  → `47 passed in 3.92s`.
- Full backend with warnings promoted to errors:
  `./.venv/Scripts/python.exe -m pytest backend -q -W error`
  → `197 passed in 5.05s`.
- Compile check:
  `./.venv/Scripts/python.exe -m compileall -q backend/app backend/tests backend/migrations`
  → PASS (exit 0, no output).
- Dependency integrity: `./.venv/Scripts/python.exe -m pip check`
  → `No broken requirements found.`
- PostgreSQL-dialect offline migration:
  `./.venv/Scripts/python.exe -m alembic -c backend/alembic.ini upgrade head --sql`
  → PASS, including feedback processing/pending fields, event/report
  idempotency constraints, and all new checks.
- `git diff --check` → PASS (exit 0; only the repository's Windows line-ending
  notices were emitted).

### Fix-round self-review

- PostgreSQL checkpoint schema ownership is delegated to the checkpoint
  library's required, idempotent `setup()`; the application does not duplicate
  private checkpoint DDL in Alembic.
- Every worker-originated durable mutation is fenced by analysis ID, worker ID,
  attempt count, and unexpired lease in the same transaction as the write.
- Terminal accept event/state/status persistence is atomic, and SSE terminal
  replay observes a single SQL snapshot, closing both sides of the race.
- Feedback commands are durable work: revision and acceptance are processed
  through the checkpointed Task 4 graph and marked consumed only after durable
  result persistence.
- Public error/validation responses are canonical; public mappings and events
  are bounded, recursively sanitized, and defensively sanitized on read.

### Fix-round concerns

- Live PostgreSQL remains NOT VERIFIED because the Docker daemon is unavailable
  (`//./pipe/docker_engine` does not exist). No live Alembic upgrade,
  multi-process `SKIP LOCKED` contention, or real PostgreSQL checkpoint setup
  was attempted. These remain Task 8 environment checks; offline PostgreSQL SQL
  generation, real async SQLite service behavior, and the production SQL/
  checkpointer seams are verified.
- No implementation finding was deferred and no review premise was left
  unchanged.
