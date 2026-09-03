# SDD ledger — plan: docs/superpowers/plans/2026-08-29-reposcope-v1.md

## 2026-09-03: safe evaluation diagnostics complete (offline only)

Based on 1c1cf54 / PR #8; branch codex/evaluation-diagnostics. Added optional host-only graph observer, closed versioned per-case diagnostic JSONL, atomic fail-closed journal and optional manifest artifact verified before gold. API/report/checkpoint/result contracts unchanged. Synthetic offline replay tests distinguish report binding, absent/invalid primary support, critique-only evidence, valid nonprimary evidence clearing, tool failures, empty search and budgets; corruption and I/O failure tests preserve safety. No paid calls, hidden data, new scores, deployment or release. Historical five-case downgrade causes remain unknown because old diagnostics were not retained. Report: docs/evaluations/safe-diagnostics-v1.md.

Verification: backend 416 passed/1 skipped; frontend 65 passed, typecheck and static build passed; schema check and release scan passed; historical v1/v2 artifact hashes unchanged. Independent final review found no Critical/Important/Minor defects and reran 54 focused tests. No repeated implementation/review loop. Next: separately approve a bounded single-development-case real diagnostic; no automatic paid rerun or hidden evaluation.

## 2026-09-03: development v2 completed; hidden gate failed

PR #6 and #7 merged into main as 7d057bc and 7c119a9. Before the authorized paid rerun, sealed mutable GitHub history in frozen evaluation only (source commit 3ee8300; regression 8 passed, full backend 394 passed/1 skipped, focused reviewer clean). One complete paired six-case run 20260903T031631Z-deepseek-v4-flash consumed 1,436,872 tokens, estimated $0.712647904 including preflight and three recovered schema failures. Offline scorer verified artifacts before loading development gold. Issue-only FileRecall@5/MRR 0.500000; RepoScope 0.166667, two valid citations in dateutil, five insufficient-evidence cases. History input policy also changed: not a single-variable causal comparison against v1. No hidden, deployment, release or second paid rerun. Report: docs/evaluations/deepseek-v4-flash-development-v2.md. Next milestone: export safe per-node diagnostics and add offline replay coverage before seeking separately bounded model diagnostics; current artifacts do not determine five downgrade causes.

## 2026-09-03: evidence propagation repair

Implementation and offline verification complete on `codex/evidence-propagation`, based on PR #6 (`f1c74c4`). Restored bounded tool navigation observations and follow-up hypotheses/gaps; bound explicit report references only to validated successful tool reads, including revision. No model calls or new benchmark scores. Backend 392 passed/1 skipped; frontend 65 passed; Chromium live 1/static 3; schema, dataset, build and security checks passed. Latest user instruction replaces repeated reviewer routing with focused regression verification. Historical six-case per-node causes remain unproven because v1 did not retain node outputs. Report: `docs/evaluations/evidence-propagation-repair-v1.md`. Next step: separately approved development rerun, not hidden.

## Preflight interface scan

| Tasks | Producer / consumer interface | Finding |
|---|---|---|
| 1 → 2 | Python package, settings, HTTP test stack | Consistent; ingestion builds on the backend skeleton. |
| 2 → 3 | Immutable `RepositorySnapshot` and normalized source paths | Consistent; Task 3 consumes only safely extracted files. |
| 3 → 4 | Read-only tool interfaces and source-location metadata | Consistent; the graph has no shell or write interface. |
| 4 → 5 | `AnalysisState`, `AnalysisReport`, events, revision command | Consistent; persistence exposes graph state through API schemas. |
| 5 → 6 | REST/SSE contracts | Consistent; web live mode consumes them while demo mode uses artifacts. |
| 6 → 7 | Demo artifact schema and observable UI events | Consistent; evaluation and portfolio docs can reuse the same report schema. |
| 7 → 8 | Test commands, benchmark/result schemas, release checklist | Consistent; final verification consumes documented commands. |
| Task 1 | Tests precede health and landing behavior; configuration is non-behavioral scaffolding | Internally consistent. |
| Task 2 | Security cases map to explicit parsing, HTTP, extraction, and limits behavior | Internally consistent. |
| Task 3 | AST fallback and seven tools preserve immutable source locations | Internally consistent. |
| Task 4 | Budgets and evidence rules have deterministic fake-model tests | Internally consistent. |
| Task 5 | Persistence, recovery, API, and SSE share analysis/event identifiers | Internally consistent. |
| Task 6 | Local live mode and public static mode are separate build-time behaviors | Internally consistent. |
| Task 7 | Metrics are deterministic; real benchmark claims remain explicitly unverified | Internally consistent. |
| Task 8 | Verification records observed evidence and does not publish externally | Internally consistent. |

Preflight: no plan/spec conflicts found.

Task 1: fix round 1/5 (5 addressed, 0 open; commits 0d08ba3..c7d8ef0)
Task 1: complete (commits bda9809..c7d8ef0, review clean)
Task 2: fix round 1/5 (8 addressed, 0 open; commits fb9fa34..20015fa)
Task 2: complete (commits c7d8ef0..20015fa, review clean)
Task 3: minor (deferred): distinguish AST load references from bindings and preserve distinct match kinds on the same line.
Task 3: minor (deferred): fail closed on os.walk traversal errors instead of silently indexing a partial tree.
Task 3: minor (deferred): split the cohesive but 638-line index module to reduce policy drift and review risk.
Task 3: minor (deferred): make index state truly immutable so the snapshot SHA cannot be reassigned after indexing.
Task 3: fix round 1/5 (2 addressed, 1 open — multiline parenthesized decorator boundary; commits 49adb89..383a023)
Task 3: fix round 2/5 (1 addressed, 0 open; commits 383a023..701e60f)
Task 3: complete (commits 20015fa..701e60f, review clean; 4 deferred minors)
Task 4: minor (deferred): expose user_revisions alongside other budget counters in observable events.
Task 4: minor (deferred): split the 742-line graph builder into focused routing/event/accounting helpers.
Task 4: fix round 1/5 (6 addressed, 1 open — malicious non-primary evidence explanation; commits 00057cc..769bc2f)
Task 4: fix round 2/5 (1 addressed, 0 open; commits 769bc2f..5e3a921)
Task 4: complete (commits 701e60f..5e3a921, review clean; 2 deferred minors)
Task 5: fix round 1/5 (9 addressed, 7 follow-up findings open; commits 8799d55..aa9fd99)
Task 5: fix round 2/5 (7 addressed, 0 open; commits aa9fd99..9177f3e)
Task 5: complete (commits 5e3a921..9177f3e, review clean; live PostgreSQL verification deferred to Task 8 because Docker was unavailable)
Task 6: complete (base 9177f3e; UI/demo delivery plus runtime hardening through the current Task 6 commit; backend 242 passed/1 skipped, frontend 65 passed, static/live builds pass, Playwright scenarios listed; browser execution deferred because local Chromium is unavailable; final review clean)
Task 7: complete (base bf9c563; versioned JSONL evaluation contracts, deterministic metrics, answer-isolated scripted runners, 12 truthful unfilled 6/6 slots, safe telemetry/cleanup regressions, Chinese-first portfolio documentation; backend 268 passed/1 skipped, frontend 65 passed, schema/type/build checks pass; no real benchmark run or score; focused rereview has no Critical or Important findings)
Task 8: complete (base a8cc704; backend 276 passed/1 skipped, frontend 65 passed, Chromium live 1/static 3, release scan passed, fresh PostgreSQL migration 20260830_0001 and Compose health passed; empty-Key worker safely degraded with no claims/model calls; independent reviewer found no Critical or Important issues; release verification and unpublished v0.1.0 draft prepared; no push/deploy/tag/release)
Post-v0.1 curation batch 1: complete (PR #2 merged as 31154da; isolated branch codex/eval-cases-01-02; 2 qualified pre-split candidates for pallets/click#2819 and python-hyper/h11#92; backend 286 passed/1 skipped, frontend 65 passed, Chromium live 1/static 3, schema/CLI/release scan passed; current and historical private-path leakage gates added; independent reviewer found no Critical or Important issues; 12 benchmark slots remain unfilled; no model run, score, fix metadata, gold, or snapshot committed)
Post-v0.1 curation batch 2: complete (PR #3 merged as 3e3e943; branch codex/eval-cases-03-04; qualified pre-split candidates for pallets/flask#2267 and pytest-dev/pluggy#544; backend 286 passed/1 skipped, frontend 65 passed, Chromium live 1/static 3, schema/CLI/release scan passed; independent reviewer found no Critical, Important, or Minor issues; 12 benchmark slots remain unfilled; no model run, score, fix metadata, gold, or snapshot committed)
Post-v0.1 curation batch 3: complete (PR #4 merged as 92f9251; qualified pre-split candidates for hynek/structlog#476 and pallets/jinja#1198; backend 286 passed/1 skipped, frontend 65 passed, Chromium live 1/static 3, schema/CLI/release scan passed; independent reviewer found no Critical, Important, or Minor issues; no model run or score)
Post-v0.1 curation final lock: complete locally (branch codex/eval-cases-07-12-lock; all 12 candidates verified; deterministic 6/6 slot v2 mapping and dataset digest de76c423ffe936743f979f35cc634caed064e571b3f5c9563b219ac5025ceea7 generated; exact public development gold separated from ignored hidden gold; official run/score bound to the frozen digest; release scan covers history/index/working tree answer leakage; backend 309 passed/1 skipped, frontend 65 passed, Chromium live 1/static 3; all reviewer findings fixed with regressions, final rereview capacity-limited and main-agent verification took over per AGENTS.md; no model run or score)
Post-v0.1 DeepSeek development evaluation: complete in open PR #6 (branch codex/deepseek-development-eval; source commit 6c85a21; run 20260902T090635Z-deepseek-v4-flash; locked digest de76c423ffe936743f979f35cc634caed064e571b3f5c9563b219ac5025ceea7; Issue-only FileRecall@5 0.666667/MRR 0.583333; RepoScope FileRecall@5 0/MRR 0 with 6/6 insufficient-evidence reports and no citations; successful run 894,436 tokens/$0.497299168 estimated; hidden not run because gates failed; reviews found retry/fail-fast/hard-cap/provenance issues, all fixed with regressions; final reviewer found no code-level Critical, Important, or Minor issues; backend 380 passed/1 skipped, frontend 65 passed, E2E live 1/static 3, schema/build/dataset/release scan passed; GitHub push CI run 33636147635 passed; machine artifacts and truthful failure report published for review)
