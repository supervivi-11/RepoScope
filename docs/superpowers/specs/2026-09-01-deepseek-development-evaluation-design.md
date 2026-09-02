# RepoScope DeepSeek Development Evaluation Design

## Goal

Run the locked six-case development benchmark with one identical DeepSeek model configuration for the issue-only baseline and RepoScope, then score both result sets only after prediction has finished. The run must be reproducible, auditable, development-only, and incapable of reading either development or hidden gold while a model is active.

## Locked experiment configuration

- Provider: DeepSeek official OpenAI-compatible API at `https://api.deepseek.com`.
- Requested model: `deepseek-v4-flash` (currently documented as DeepSeek-V4-Flash-0731).
- Thinking: enabled with `reasoning_effort=low`. Real pre-runs showed that `high` consumed both 4096 and 8192 output-token ceilings on simple structured tasks without producing a final JSON response; the fixed scored run therefore uses `low` for both systems.
- Temperature: omitted because DeepSeek ignores sampling parameters in thinking mode.
- Split: exactly the six locked `development` cases.
- Dataset digest: read from and verified against `evals/benchmark-cases.v1.sha256` before any request.
- Agent budget: at most 12 read-only tool calls, two evidence rounds, and two model retries.
- Output-token budget: 8192 per request, fixed in the versioned run configuration and identical wherever the two systems perform the same report task. It remains a ceiling; measured usage determines cost.
- API credentials: `REPOSCOPE_DEEPSEEK_API_KEY` only; never accepted as CLI arguments or serialized.

The issue-only baseline receives only repository identity, issue number, title, and body. RepoScope additionally receives the verified pre-fix snapshot through the existing static index and bounded read-only investigation tools. Neither system sees a fixing PR, diff, post-fix source, development gold, or hidden gold.

## Provider boundary

The production evaluation adapter uses the DeepSeek Responses API through an OpenAI-compatible client. Before the real benchmark it runs an unscored synthetic compatibility preflight for every structured response model used by the graph. If Responses JSON Schema is incompatible, the whole experiment must be reconfigured before predictions begin; per-case or per-system fallback is forbidden.

Every call records only operational metadata: requested and returned model identifiers, system fingerprint when provided, UTC timestamps, latency, input tokens, cached input tokens, output tokens, reasoning tokens, retry count, and safe error code. Hidden reasoning content and credentials are never persisted.

## Prediction and gold isolation

Online prediction is a separate CLI entry point and module. It has no `--gold` argument and does not import benchmark gold contracts, scoring code, or gold paths. It accepts only the locked case catalog, digest file, development snapshot root, versioned configuration, and output directory.

At process startup it installs a fail-closed file-read guard for development and hidden gold names. It rejects `hidden` and `all`, validates that exactly six development cases are selected, verifies every pre-fix snapshot digest, and writes results atomically. Issue-only and RepoScope files must each contain exactly one result for every development case with the same dataset digest and configuration fingerprint.

After the prediction process exits, an offline scoring command receives `development-gold.v1.jsonl` explicitly. It verifies the prediction manifest before reading gold. Hidden gold is never accepted by the development scoring command.

## Results and provenance

The run directory contains:

- redacted `run-config.json` and exact reproduction commands;
- per-call usage ledger JSONL;
- raw issue-only and RepoScope result JSONL;
- SHA-256 manifest covering configuration and result artifacts;
- development `summary.v2.json` with FileRecall@5, MRR, citation validity, hallucinated citation rate, latency, tokens, and estimated cost;
- a human-readable run report that distinguishes measured results from targets.

The requested alias, provider-returned model, system fingerprint, RepoScope commit, dependency versions, dataset digest, runner/prompt version, run ordering, and UTC window are retained. DeepSeek aliases are rolling, so the report promises reproducible inputs and configuration, not byte-identical future outputs.

## Cost accounting

Cost is calculated per request from provider usage rather than inferred from text length:

`cached_input * cache_hit_rate + uncached_input * cache_miss_rate + output * output_rate`.

The versioned rate card records peak/off-peak prices and its official-source retrieval date. Each request is assigned a rate period using its UTC start time. Missing usage makes cost `null`; it is never guessed. Aggregate usage remains available in each benchmark result and summary.

## Failure behavior

- Missing credentials fail before any request.
- Dataset, split, snapshot, or configuration mismatch fails before any request.
- Partial outputs remain clearly marked and cannot be scored as a complete comparison.
- Provider errors use bounded retries and safe codes without response bodies or secrets.
- Configuration or fingerprint drift is reported, not silently normalized.
- No hidden run, score, or readiness claim is produced by this milestone.
