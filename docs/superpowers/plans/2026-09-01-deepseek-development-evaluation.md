# RepoScope DeepSeek Development Evaluation Implementation Plan

> **Execution:** Follow test-driven development for every behavior change. Run focused tests after each task, the full suite once before review, and once after material review fixes.

**Goal:** Implement and execute the six-case DeepSeek Flash development comparison without exposing gold during prediction, then publish auditable real results in a reviewed pull request.

**Architecture:** A provider-specific, usage-recording model gateway feeds both a one-call issue-only predictor and the existing bounded RepoScope graph. A development-only prediction process writes atomic artifacts without importing or reading gold. A separate offline scorer verifies the prediction manifest before loading development gold.

**Spec:** `docs/superpowers/specs/2026-09-01-deepseek-development-evaluation-design.md`

## Task 1: Versioned experiment contracts

- Add failing contract and Schema tests for the fixed DeepSeek configuration, per-call usage ledger, artifact manifest, and summary provenance.
- Extend evaluation contracts without breaking v1 scripted results.
- Add deterministic configuration fingerprints and JSON Schemas.
- Verify focused contract/schema tests.

## Task 2: DeepSeek structured gateway and usage recorder

- Add failing tests for environment-only credentials, official base URL, Flash model lock, thinking configuration, omitted temperature, output budget, zero SDK retries, safe errors, and raw usage extraction.
- Implement a DeepSeek Responses adapter that satisfies the existing `ModelGateway` protocol and records metadata without reasoning content.
- Add an unscored structured-schema preflight for issue understanding, tool request, critique, report, and issue-only prediction output.
- Verify gateway and compatibility tests with fake clients only.

## Task 3: Real predictors

- Add failing tests for an issue-only predictor that receives no source or gold fields and returns ranked file guesses.
- Add failing tests for a RepoScope analyzer that builds the existing static index/tools/graph from only the verified pre-fix snapshot and converts a validated report into an evaluation prediction.
- Aggregate latency, calls, tokens, retries, tool count, fingerprints, and cost from the shared recorder.
- Verify predictors with a deterministic fake gateway.

## Task 4: Development-only prediction process

- Add failing subprocess/CLI tests proving `hidden` and `all` are rejected, exactly six development cases are required, the locked digest and snapshot digests are checked before provider construction, and API keys cannot be supplied by CLI.
- Add tests that attempt to open both gold files while prediction is active and assert fail-closed denial.
- Implement the standalone prediction module with no gold/scorer imports, a file-read guard, atomic per-system JSONL output, redacted configuration, call ledger, and SHA-256 manifest.
- Run issue-only and RepoScope in deterministic paired order and reject incomplete or configuration-mismatched output.

## Task 5: Offline development scoring and reproducibility artifacts

- Add failing tests that the development scorer starts only from complete manifested predictions, accepts only development gold, validates snapshots/citations, and emits summary v2 plus exact commands.
- Implement per-request DeepSeek peak/off-peak cost calculation using a versioned rate card; make missing usage produce `null` cost.
- Add empty real-run templates and update `evals/README.md` without claiming scores.
- Verify all evaluation tests.

## Task 6: Real compatibility preflight and development run

- Confirm `REPOSCOPE_DEEPSEEK_API_KEY` is configured locally without printing it.
- Run the synthetic Responses/JSON-Schema compatibility preflight and record its safe result.
- Re-run the locked dataset and six development snapshot checks.
- Execute paired issue-only and RepoScope predictions once with the approved fixed configuration.
- Exit the online process, then score with `development-gold.v1.jsonl` and verify every artifact hash.
- Record only measured results, failures, usage, cost, and provider drift information.

## Task 7: Verification, review, and pull request

- Run backend, frontend, schema, release-security, and relevant end-to-end tests.
- Scan the diff and tracked artifacts for credentials, snapshots, hidden gold, and fabricated claims.
- Request independent review of the final diff; fix all Critical or Important findings with regression tests and re-review material fixes.
- Update the durable progress ledger and real evaluation report.
- Commit with Conventional Commits, push the feature branch, wait for GitHub CI, and create a pull request.
- Audit every goal requirement and stop after explaining the real comparison, failed cases, and whether development evidence justifies a future hidden run.

