## Task 7 report: evaluation, security, and portfolio documentation

### Outcome

Implemented the versioned offline evaluation subsystem, deterministic metrics, answer-isolated runners, operational telemetry and portfolio documentation without making GitHub/model calls or claiming benchmark results. The twelve committed benchmark records remain truthful metadata-only `unfilled` slots with a fixed six/six development/hidden allocation.

Delivery commit: `docs: prepare reposcope evaluation and release` (this report is included in that commit).

### Evaluation contracts and runners

- Added strict top-level versions for slot, case, gold, scripted prediction, result and summary contracts. Checked-in JSON Schemas are generated from Pydantic and have a drift check.
- Added bounded canonical JSONL I/O: UTF-8 without BOM, duplicate-key rejection, finite JSON only, explicit row/file limits, composite `(case_id, system)` identities where required, deterministic sorting, trailing newline and atomic replacement.
- Added `IssueOnlyRunner`, which exposes only repository identity and frozen Issue text to its predictor.
- Added `RepoScopeRunner`, which verifies the pre-fix snapshot tree digest, copies it into a runner-owned temporary directory, never deletes the external source, and cleans its copy on success, safe failure or cancellation.
- Added scripted CLI adapters for deterministic no-network pipeline validation. They are explicitly not presented as real model evaluation.
- Added result failure rows with canonical safe codes; exception text is never serialized.
- Added reproducible `validate-slots`, `run` and `score` commands plus `results-empty.v1.jsonl`.

### Deterministic metrics and answer isolation

- `FileRecall@5` uses the fraction of 1–5 gold production paths found in the first five ordered predictions.
- MRR uses the first gold path rank across the full ordered prediction list.
- Failed cases contribute zero to retrieval metrics, preventing survivorship bias.
- Citation validity reuses the production validator against the matching pre-fix index; wrong commit, path, range or excerpt is a hallucinated citation.
- Zero emitted citations yield null validity/hallucination rates, not an artificial 100%.
- Missing or duplicate system/case rows are errors; paired deltas are emitted only for complete Issue-only/RepoScope pairs.
- Every case requires a commit-matching pre-fix index and every gold file must exist in that snapshot before scoring.
- Latency, Token and cost aggregates remain null until all rows contain observed values. Cost requires actual Token values and a versioned rate card.
- Case/result and gold contracts are structurally separate. Hidden gold is documented as evaluator-only and ignored under the local evaluation directory.

### Dataset slots and truthfulness

- Added exactly `dev-01`–`dev-06` and `hidden-01`–`hidden-06` as `unfilled` slots.
- Documented historical Bug eligibility, pre-fix-only inputs, production-file gold rules, one-case-per-repository policy, deterministic SHA-256 split procedure and post-result immutability.
- No repository/Issue metadata was invented. Filling slots requires later manual verification of a real merged fix.
- No real model was run and no score, latency, Token or cost result was created or implied.
- The three Task 6 `example/*` artifacts remain visibly labeled product walkthrough placeholders, insufficient-evidence cases and non-benchmark output.

### Operational safety and observability

- Added allowlisted JSON telemetry with UTC timestamp, service/event identity, monotonic duration and safe counters/status/error codes.
- Worker supervision emits one terminal attempt observation for normal results, handled failures, exhausted attempts and escaped failures without prompt, Issue, excerpt, exception, credential or snapshot-path content.
- Snapshot janitor emits only duration and removed count; existing active/TTL/path safety behavior is unchanged.
- Evaluation snapshot hashing rejects symlinks, escapes, files above 500,000 bytes and trees above 10,000,000 bytes.
- Added cleanup regressions for success, failure, cancellation, digest mismatch, oversized files and external-source preservation.

### Portfolio documentation

- Replaced the root README with a Chinese-first product overview, English summary, local startup, test commands, evaluation truthfulness and portfolio links.
- Added architecture, security policy, contribution guide, demo recording guide, five-minute interview narrative, ten interview questions and a release checklist.
- Documented why RepoScope is not ordinary RAG, why tools are read-only, how AST/keyword/vector retrieval differ, how checkpoint/SSE cooperate, why PRs are evaluator-only truth, why LLM-as-judge is insufficient, how citations are validated and why the public Demo is static.

### Reviewer findings and resolution

Initial independent review found no Critical and four Important issues:

1. Paired system rows collided on `case_id` — fixed with composite identity and canonical sort.
2. Nonexistent gold paths could score — fixed by mandatory snapshot/index and gold existence validation.
3. A schema-valid oversized row could be written but not read — fixed with one shared 1,000,000-byte payload limit in model, writer and reader.
4. Missing version markers and string issue numbers were accepted — fixed with required versions, regenerated Schemas and strict issue-number validation.

Two Minor issues were also fixed: handled failure codes now appear in safe telemetry, and `/local/` evaluation artifacts are ignored. Focused rereview reported no new Critical or Important findings. Its final exact-row delimiter Minor was fixed by defining the limit over the JSON payload, excluding the JSONL newline, with exact-limit and limit-plus-one tests.

### Final verification

- Backend: `268 passed, 1 skipped` with warnings treated as errors. The existing skip is the Windows symlink-root test when symlink creation is unavailable.
- Task 7 evaluation tests: `23 passed` after the final boundary fix.
- Frontend: `65 passed`, 11 files.
- Frontend typecheck: PASS.
- Build configuration isolation: PASS.
- Static and live Vite builds: PASS earlier in the final Task 7 verification cycle; generated `dist` was removed afterward.
- Playwright discovery: PASS, four Chromium scenarios listed.
- Python compileall: PASS.
- `pip check`: PASS, no broken requirements.
- Frontend report-limit generator check: PASS.
- Evaluation Schema drift check: PASS.
- Slot catalog validation: PASS, 12 slots with 6/6 split.
- `git check-ignore local\hidden-gold.v1.jsonl`: PASS.
- `git diff --check`: PASS (Windows line-ending notices only).

### Explicitly not executed

- No real GitHub, OpenAI-compatible model, embedding, paid API or benchmark run.
- No historical case curation and no benchmark score.
- No live PostgreSQL/Docker integration or migration execution; those belong to Task 8.
- No Playwright browser execution; this environment still lacks the Chromium binary recorded in Task 6. Only test discovery ran.
- No push, deployment, Release publication or Task 8 integration verification.
