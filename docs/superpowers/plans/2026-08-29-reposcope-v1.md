# RepoScope v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Build a tested, local-first RepoScope v1 that turns a public Python GitHub bug issue into an evidence-backed static investigation report and ships a static public demo.

**Architecture:** A FastAPI/LangGraph backend securely ingests immutable GitHub source snapshots, indexes Python code, runs a bounded read-only investigation, validates evidence, persists jobs in PostgreSQL, and streams progress over SSE. A React application drives local analyses and replays committed demo artifacts without a public model backend.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, LangGraph, PostgreSQL 16, pgvector, React, TypeScript, Vite, TanStack Query, Pytest, Vitest, Playwright, Docker Compose, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-29-reposcope-v1-design.md`

## Global Constraints

- Public Python GitHub repositories only.
- Never execute repository code, install repository dependencies, modify repository content, create commits, or open pull requests.
- Never expose shell or arbitrary-network tools to the agent.
- Every code citation is validated against an immutable commit snapshot.
- The public website replays pre-generated artifacts and never calls a model backend.
- Tests are written first and observed failing before production behavior is added.

---

## Task 1: Project foundation and health vertical slice

Create the backend and frontend package skeletons, Docker Compose with pgvector, configuration templates, database migration skeleton, CI, license, and initial README. Start with failing backend and frontend health/render tests. Implement `GET /health` returning `{"status":"ok","service":"reposcope-api"}` and a React landing page that identifies RepoScope and its static-read-only boundary. Provide commands for local development and tests. Commit as `feat: scaffold reposcope application`.

## Task 2: Secure GitHub ingestion

Test-first implement strict GitHub URL parsing, positive issue validation, repository metadata limits, an async read-only GitHub REST client, immutable commit resolution, safe codeload archive extraction, ignore rules, source byte/file limits, symlink rejection, and snapshot cleanup metadata. Mock only outbound HTTP. Cover private/missing/oversized repositories, rate limits, timeouts, ZIP Slip, symlinks, binary files, and oversized source. Commit as `feat: add secure github ingestion`.

## Task 3: Python static index and investigation tools

Test-first implement AST symbols/imports with text fallback, lexical search, chunk metadata ready for embeddings, repository maps, and the seven read-only tool interfaces in the spec. Source results must include immutable commit, normalized relative path, exact lines, and excerpts. No subprocess or dynamic import is permitted. Commit as `feat: add python repository investigation tools`.

## Task 4: Agent graph and evidence validation

Test-first define report and graph state models, budgets, model retry policy, deterministic citation validation, bounded issue-understanding/investigation/critique/report nodes, and one revision path. Use a fake model for deterministic tests. A primary hypothesis without valid evidence must become an explicit insufficient-evidence result. Commit as `feat: add bounded investigation graph`.

## Task 5: Persistent analyses API and SSE

Test-first implement SQLAlchemy models and Alembic migration, a PostgreSQL-backed claimable job queue with idempotency, LangGraph-compatible checkpoint configuration, analysis/feedback/demo endpoints, SSE event replay using monotonic sequence numbers, restart recovery, and error serialization without secrets. Commit as `feat: add persistent analysis api`.

## Task 6: React investigation UI and static demo mode

Test-first implement the analysis form, status timeline, evidence viewer, hypotheses, impacted files, implementation steps, proposed tests, uncertainties, accept/revise controls, SSE reconnect, errors, and responsive styling. Add three clearly labeled pre-generated demo artifacts and ensure static demo mode performs no API/model request. Add Playwright coverage. Commit as `feat: add investigation workspace and demos`.

## Task 7: Evaluation, security, and portfolio documentation

Implement a JSONL evaluation contract, deterministic FileRecall@5/MRR/citation metrics, issue-only versus RepoScope runners, twelve metadata-only benchmark slots with documented selection criteria and split rules, result schema, and commands. Add structured logs, metrics, cleanup, security tests, Chinese README with English summary, architecture documentation, SECURITY, CONTRIBUTING, demo recording guide, interview notes, and release checklist. Do not fabricate benchmark results or claim targets were achieved without a real model run. Commit as `docs: prepare reposcope evaluation and release`.

## Task 8: Integration verification and release candidate

Run backend, frontend, Playwright, Docker Compose, migration, static-demo no-network, secret scan, and full repository checks. Fix failures through failing regression tests. Produce `docs/release-verification.md` with exact commands and observed outputs, ensure the worktree is clean, and prepare but do not publish or push the `v0.1.0` release. Commit as `chore: verify reposcope release candidate`.

