# Task 4 report — Agent graph and evidence validation

## Status

DONE

## RED/GREEN evidence

### Cycle 1 — immutable contracts, budgets, and exact citation validation

- RED: `../.venv/Scripts/python.exe -m pytest tests/test_agent_models.py tests/test_evidence_validation.py -q`
  from `backend/` failed during collection with `ModuleNotFoundError: No module
  named 'app.agent'`. The report/event/budget contracts and pure evidence
  validation interface did not exist.
- GREEN: after adding frozen, extra-forbidding Pydantic models plus exact
  `PythonRepositoryIndex.read_code` validation and deterministic downgrade,
  the same selection produced `11 passed in 0.09s`.

### Cycle 2 — structured model gateway and seven-tool dispatcher

- RED: the focused gateway/dispatcher selection failed during collection
  because `ModelPhase` and `ALLOWED_TOOL_NAMES` were absent.
- GREEN: typed transient-only retries, the environment-configured thin
  OpenAI-compatible adapter, explicit argument schemas, exact allowlisting,
  deterministic source extraction, and generic safe tool failures produced
  `10 passed in 0.17s`.

### Cycle 3 — bounded LangGraph and review workflow

- RED: `../.venv/Scripts/python.exe -m pytest tests/test_agent_graph.py -q`
  failed during collection because `build_analysis_state` and
  `build_investigation_graph` did not exist.
- Intermediate RED: after the minimal graph was added, three tests failed
  because an insufficient critique was incorrectly represented as an invalid
  incomplete `ToolRequest`. This proved the request schema was enforcing the
  boundary and exposed a routing-state design error.
- GREEN: a separate `critique_sufficient` state flag produced `6 passed in
  0.50s` for happy pause/accept, twelve calls, two rounds, report downgrade,
  one revision/second rejection, and safe tool failure events.

### Cycle 4 — monotonic deterministic events

- RED: the twelve-call graph regression failed because the fifteenth event
  reused sequence `14` when one node emitted both tool completion and budget
  exhaustion.
- GREEN: node-local sequence offsets made every observable sequence strictly
  monotonic; the focused regression produced `1 passed in 0.33s`.

### Cycle 5 — fail-closed raw feedback and path-prefix validation

- RED: two focused regressions failed: an escaping `search_code.path_prefix`
  became a runtime tool failure instead of an argument rejection, and malformed
  raw revision feedback incorrectly completed the report.
- GREEN: schema validation now rejects the path before invocation, and invalid
  resume data emits `feedback_rejected` then re-pauses for review; the focused
  selection produced `2 passed in 0.33s`.

### Cycle 6 — failed revision history

- RED: the permanent revision-failure regression failed with missing
  `original_report`; the generic failure update had also been overwritten by a
  revision-start event tuple.
- GREEN: the failure path now preserves the original report/history, consumes
  exactly one revision, emits only a generic model error, and re-pauses; the
  focused regression produced `1 passed in 0.32s`.

## Verification

- Final focused Task 4 suite:
  `./.venv/Scripts/python.exe -m pytest backend/tests/test_agent_models.py backend/tests/test_evidence_validation.py backend/tests/test_agent_gateway.py backend/tests/test_agent_tool_dispatch.py backend/tests/test_agent_graph.py -q`
  → `29 passed in 0.63s`, no warnings.
- LangGraph review/resume suite with warning logging enabled:
  `../.venv/Scripts/python.exe -m pytest tests/test_agent_graph.py -q -o log_cli=true --log-cli-level=WARNING`
  → `8 passed in 0.59s`, no warnings. The in-memory checkpoint serializer uses
  an explicit allowlist for the Task 4 state models.
- Final full backend suite:
  `./.venv/Scripts/python.exe -m pytest backend/tests -q`
  → `139 passed in 1.36s`, no skips or warnings.
- Compile check: `./.venv/Scripts/python.exe -m compileall -q backend/app`
  → PASS.
- Dependency compatibility: `./.venv/Scripts/python.exe -m pip check`
  → `No broken requirements found.`
- `git diff --cached --check` → PASS before commit.
- Post-commit `git status --short` → clean.

## Files changed

- `.env.example`
- `backend/pyproject.toml`
- `backend/app/agent/__init__.py`
- `backend/app/agent/gateway.py`
- `backend/app/agent/graph.py`
- `backend/app/agent/models.py`
- `backend/app/agent/tool_dispatch.py`
- `backend/app/agent/validation.py`
- `backend/app/investigation/tools.py`
- `backend/tests/test_agent_gateway.py`
- `backend/tests/test_agent_graph.py`
- `backend/tests/test_agent_models.py`
- `backend/tests/test_agent_tool_dispatch.py`
- `backend/tests/test_evidence_validation.py`

## Commit

`00057cc19ed31ffff0bd039e60199ed13842064f` —
`feat: add bounded investigation graph`

## Self-review

- Every required Task 4 public contract is frozen and rejects extra fields.
  Evidence carries immutable SHA/path/range/excerpt identity; confidence is
  constrained to `[0,1]`; event fields cannot contain prompts, raw messages,
  credentials, or reasoning.
- `AnalysisState` keeps frozen analysis/repository/issue identity and explicitly
  bounds hypotheses, evidence, tool history, errors, events, report history,
  revision feedback, and all counters. Model output never owns a counter or
  budget field.
- `InvestigationBudget` defaults to twelve tool attempts, two evidence rounds,
  two retries after the first model attempt, and one revision. Validation
  rejects every value beyond those v1 maxima.
- Evidence validation is pure and independently exported. It checks the
  snapshot SHA first, performs the exact normalized `read_code` request, and
  compares commit/path/range/excerpt without rewriting source. Invalid evidence
  is removed and becomes a safe citation-summary event.
- A primary hypothesis without at least one valid supporting citation becomes
  `insufficient_evidence`, loses deterministic implementation steps/impacted
  files, receives confidence at most `0.25`, and records what remains unknown.
- The model boundary accepts only typed Pydantic results. Only
  `TransientModelError` retries, with at most three total attempts; schema,
  safety, and permanent failures never retry and never surface raw exception
  text.
- The dispatcher exposes exactly the Task 3 seven-tool allowlist. Every tool has
  a dedicated argument schema, invalid names/arguments never invoke the facade,
  and invocation exceptions become a bounded generic uncertainty/event.
- The LangGraph topology covers issue understanding, evidence rounds, tool
  selection/execution, critique, composition, deterministic validation,
  checkpointed review pause, acceptance, one revision, second-revision
  rejection, and malformed-feedback rejection. Events are stable and strictly
  monotonic.
- Tests inject deterministic scripted models and fake GitHub access. No test
  loads credentials, starts a provider adapter, or uses the network.
- No database model, API route, frontend code, shell tool, subprocess,
  repository execution, arbitrary URL, or write capability was added.

## Concerns

No Task 4 blocker. Task 5 must inject its persistent checkpoint implementation
and serializer policy; Task 4 intentionally uses only explicitly allowlisted
LangGraph in-memory checkpointing in tests.

## Fix Round 1 — review hardening

### Status

DONE

### RED/GREEN evidence

- Baseline: the preserved uncommitted combined-budget regression and graph
  routing change were inspected before editing. The first focused Task 4 run
  produced `30 passed, 1 failed`; the failure proved an unsupported,
  model-provided tool name was copied verbatim into an observable event.
  GREEN: unsupported names now become the fixed `unknown` value everywhere
  observable, and the hostile prompt/secret regression passes.
- No-evidence downgrade RED: a report whose summary, observed/expected text,
  primary/alternative hypotheses, impact explanation, implementation step,
  proposed test, and uncertainty contained a fabricated secret claim retained
  that claim after downgrade. GREEN: the downgrade now replaces every
  free-text field with canonical insufficient-evidence text, clears all
  hypotheses/impact/steps/tests, emits the canonical uncertainty, and caps
  confidence at `0.25`.
- Gateway RED: three regressions showed failed invocations did not expose their
  actual attempt count and an OpenAI timeout was classified permanent. GREEN:
  the adapter sets `max_retries=0`, maps OpenAI timeout/connection/rate-limit
  errors to `TransientModelError`, and records actual attempts for success,
  zero-retry failure, transient-then-permanent failure, and graph events.
- Path dispatch RED: drive, whitespace-padded, NUL, dot/traversal, and invalid
  separator paths could reach the facade. GREEN: Task 3's shared normalizer is
  now used for tools and evidence, rejects all of those inputs before facade
  invocation, and canonicalizes valid backslashes deterministically.
- Citation extraction RED: a tool result with 40 source excerpts raised
  `ToolResultSummary` validation instead of completing safely. GREEN:
  extraction deduplicates, sorts by the complete citation identity, and caps
  at 32 before constructing the summary.
- Immutable requests RED: `ToolRequest.arguments` was a mutable arbitrary
  dictionary with no checkpoint-safe typed representation. GREEN: it is now a
  frozen tuple of unique, sorted, flat JSON-scalar key/value entries; mutation,
  duplicate keys, nested/non-serializable values, and unsafe round trips are
  rejected by regressions.

### Verification

- Focused Task 4 suite:
  `../.venv/Scripts/python.exe -m pytest tests/test_agent_models.py tests/test_evidence_validation.py tests/test_agent_gateway.py tests/test_agent_tool_dispatch.py tests/test_agent_graph.py -q`
  from `backend/` → `40 passed in 1.84s`.
- Full backend suite:
  `./.venv/Scripts/python.exe -m pytest backend/tests -q` from the worktree
  root → `150 passed in 2.54s`.
- Compile check:
  `./.venv/Scripts/python.exe -m compileall -q backend/app` → PASS.
- Dependency compatibility:
  `./.venv/Scripts/python.exe -m pip check` → `No broken requirements found.`

### Files changed

- `backend/app/agent/__init__.py`
- `backend/app/agent/gateway.py`
- `backend/app/agent/graph.py`
- `backend/app/agent/models.py`
- `backend/app/agent/tool_dispatch.py`
- `backend/app/agent/validation.py`
- `backend/app/investigation/index.py`
- `backend/tests/test_agent_gateway.py`
- `backend/tests/test_agent_graph.py`
- `backend/tests/test_agent_models.py`
- `backend/tests/test_agent_tool_dispatch.py`
- `backend/tests/test_evidence_validation.py`

### Self-review

- The default graph cannot enter a second evidence round once either default
  budget is exhausted; its combined-budget regression verifies exactly twelve
  selections and one critique.
- No rejected model tool name, prompt fragment, or secret can enter an event,
  history item, citation, or safe error.
- The single Task 3 normalizer now rejects raw dot/empty path segments before
  `PurePosixPath` could collapse them; tool and evidence contracts share it.
- `ToolArgument` is an explicitly exported frozen contract, is included in the
  in-memory checkpoint serializer allowlist, and survives JSON round trips.
- The staged diff check below excludes the unrelated generated `backend/.idea`
  directory.

### Concerns

No Task 4 blocker. `backend/tests/test_migrations.py` expects the worktree-root
relative `backend/alembic.ini`, so full backend verification must run from the
worktree root (where it passes), not from `backend/`.

## Fix Round 2 — no-evidence secondary citation leak

### Status

DONE

### RED/GREEN evidence

- RED: `../.venv/Scripts/python.exe -m pytest tests/test_agent_graph.py -q`
  from `backend/` produced `11 passed, 1 failed`. A report with fabricated
  primary evidence and a structurally valid secondary citation retained the
  secondary citation's model-authored `ROOT CAUSE`/`sk-secret` explanation
  after being downgraded to insufficient evidence.
- GREEN: insufficient-evidence downgrade now clears report evidence entirely,
  in addition to the existing canonical text/hypothesis/impact/step/test
  clearing. The graph regression serializes the complete report plus events
  and proves neither hostile fragment remains.

### Verification

- Focused Task 4 suite:
  `../.venv/Scripts/python.exe -m pytest tests/test_agent_models.py tests/test_evidence_validation.py tests/test_agent_gateway.py tests/test_agent_tool_dispatch.py tests/test_agent_graph.py -q`
  → `41 passed in 1.74s`.
- Full backend suite from the worktree root:
  `./.venv/Scripts/python.exe -m pytest backend/tests -q` → `151 passed in
  2.52s`.
- `./.venv/Scripts/python.exe -m compileall -q backend/app` → PASS.
- `./.venv/Scripts/python.exe -m pip check` → `No broken requirements found.`

### Files changed

- `backend/app/agent/validation.py`
- `backend/tests/test_agent_graph.py`

### Self-review

- An insufficient-evidence report now preserves no evidence citations, so a
  valid-but-nonprimary citation cannot retain model-authored explanations.
- Validation events retain only `EvidenceCitationSummary`, which contains no
  explanation text; the regression checks the serialized report and all
  serialized events together.

### Concerns

No Task 4 blocker. The generated, unrelated `backend/.idea` directory remains
untracked and excluded from this fix commit.
