# RepoScope v1 Design

## Product goal

RepoScope turns a public Python GitHub bug issue into an evidence-backed investigation report. It helps developers understand an unfamiliar repository; it does not execute code, modify files, install dependencies, create commits, or open pull requests.

## Locked scope

- Input: `https://github.com/{owner}/{repo}` plus a positive issue number.
- Source: public GitHub repositories only.
- Language: Python only for v1.
- Public website: three pre-generated, clearly labeled demo cases; no live model backend.
- Local mode: users provide their own OpenAI-compatible chat and embedding credentials.
- Safety: GitHub/API/codeload allowlist, safe archive extraction, no symlink extraction, no repository code execution, and no shell tool exposed to the agent.
- Limits: repository metadata size at most 50 MB, indexed source at most 10 MB, individual file at most 500 KB, twelve agent tool calls, two evidence-gathering rounds, two model retries, and one user revision.

## Architecture

The FastAPI service persists analysis jobs in PostgreSQL, executes a LangGraph workflow, and streams observable events through SSE. A secure GitHub ingestion component resolves an immutable commit and downloads a source archive. A Python indexer builds AST symbol/import metadata, lexical search data, and optional pgvector embeddings. The React application consumes the API in local mode and replays committed JSON artifacts in public demo mode.

The graph validates input, ingests the repository, builds the repository map, understands the issue, investigates through bounded read-only tools, critiques evidence, validates citations, composes the report, and pauses for acceptance or one revision. The user interface shows tool events and evidence, never hidden chain-of-thought.

## Read-only tools

- `get_repository_map()`
- `search_code(query, path_prefix=None)`
- `read_code(path, start_line, end_line)`
- `find_symbol(symbol)`
- `find_references(symbol)`
- `get_recent_commits(path=None)`
- `get_related_issues(query)`

Every source result includes path, line range, and commit SHA. GitHub history tools use only read-only REST endpoints.

## Report contract

`AnalysisReport` contains the issue summary, observed and expected behavior, a primary hypothesis, alternative hypotheses, evidence, impacted files, implementation steps, proposed tests, uncertainties, and overall confidence. Evidence contains path, start/end lines, commit SHA, exact excerpt, and explanation.

Before persistence, citations are deterministically checked against the immutable snapshot. A primary hypothesis without valid evidence is rejected. Unknowns remain explicit uncertainties.

## API contract

- `POST /api/v1/analyses`
- `GET /api/v1/analyses/{id}`
- `GET /api/v1/analyses/{id}/events`
- `POST /api/v1/analyses/{id}/feedback`
- `GET /api/v1/demo-cases`
- `GET /health`

Statuses are `QUEUED`, `INGESTING`, `INDEXING`, `INVESTIGATING`, `REVIEW_READY`, `REVISING`, `COMPLETED`, and `FAILED`.

## Evaluation

Twelve historical Python bugs with merged fixes are split six/six into development and hidden sets. The agent sees the pre-fix commit and issue but never the fixing PR or diff. RepoScope is compared with an issue-only model on FileRecall@5, MRR, citation validity, hallucinated citations, evidence completeness, latency, tokens, and estimated cost.

Targets are 100% valid citations, zero hallucinated file/line references, hidden FileRecall@5 of at least 70%, at least twenty percentage points over baseline, and median completion under three minutes.

## Out of scope

Java and private repositories, OAuth, code execution, dependency installation, automatic patches or PRs, multi-agent workflows, accounts, billing, team permissions, and a public live-model backend are excluded from v1.

