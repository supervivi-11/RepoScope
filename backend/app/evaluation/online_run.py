from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

from app.agent import (
    AnalysisReport,
    CritiqueResult,
    IssueUnderstanding,
    ModelGateway,
    ModelGatewayError,
    ModelPhase,
    ToolRequest,
    invoke_structured,
)
from pydantic import ValidationError

from .catalog import dataset_digest, load_case_catalog
from .contracts import BenchmarkCase, BenchmarkResult
from .errors import EvaluationRunAbort
from .jsonl import write_jsonl
from .predictors import IssueOnlyModelOutput
from .real_contracts import (
    ArtifactDigest,
    DependencyVersion,
    DeepSeekRunConfig,
    ProviderCallUsage,
    RunArtifactManifest,
    REQUIRED_DEPENDENCIES,
    configuration_digest,
)
from .real_validation import validate_complete_prediction_evidence
from .snapshot import snapshot_tree_digest
from .diagnostics import DiagnosticJournal, DIAGNOSTIC_FILENAME, validate_diagnostic_artifact


_GOLD_FILENAMES = frozenset(
    {"development-gold.v1.jsonl", "hidden-gold.v1.jsonl"}
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reposcope-development-predict")
    parser.add_argument("--split", choices=("development",), default="development")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--dataset-digest-file", required=True)
    parser.add_argument("--snapshots-root", required=True)
    parser.add_argument("--output-directory", required=True)
    return parser


def install_gold_read_guard(
    protected_paths: Sequence[Path] = (),
) -> None:
    """Deny direct or indirect reads of either answer file in this process."""

    protected_resolved: set[Path] = set()
    protected_identities: set[tuple[int, int]] = set()
    for path in protected_paths:
        resolved = path.resolve(strict=False)
        protected_resolved.add(resolved)
        try:
            stat = resolved.stat()
        except OSError:
            continue
        protected_identities.add((stat.st_dev, stat.st_ino))

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event != "open" or not arguments:
            return
        raw = arguments[0]
        if isinstance(raw, bytes):
            try:
                raw = os.fsdecode(raw)
            except UnicodeDecodeError:
                return
        if not isinstance(raw, (str, os.PathLike)):
            return
        candidate = Path(raw)
        parts = {part.casefold() for part in candidate.parts}
        resolved = candidate.resolve(strict=False)
        resolved_parts = {part.casefold() for part in resolved.parts}
        identity: tuple[int, int] | None = None
        try:
            stat = resolved.stat()
            identity = (stat.st_dev, stat.st_ino)
        except OSError:
            pass
        if (
            parts.intersection(_GOLD_FILENAMES)
            or resolved_parts.intersection(_GOLD_FILENAMES)
            or resolved in protected_resolved
            or (identity is not None and identity in protected_identities)
        ):
            raise PermissionError("benchmark gold is unavailable during prediction")

    sys.addaudithook(audit)


def load_development_inputs(
    *,
    cases_path: Path,
    digest_path: Path,
    snapshots_root: Path,
) -> tuple[tuple[BenchmarkCase, ...], str]:
    cases = load_case_catalog(cases_path)
    expected = _read_digest(digest_path)
    actual = dataset_digest(cases)
    if actual != expected:
        raise ValueError("case catalog does not match the locked digest")
    development = tuple(case for case in cases if case.split == "development")
    if len(development) != 6:
        raise ValueError("prediction requires exactly six development cases")
    root = snapshots_root.resolve(strict=True)
    if snapshots_root.is_symlink() or not root.is_dir():
        raise ValueError("snapshot root must be a real directory")
    for case in development:
        snapshot = root / case.case_id
        if snapshot_tree_digest(snapshot) != case.snapshot_tree_digest:
            raise ValueError("development snapshot does not match the locked digest")
    return development, actual


CaseExecutor = Callable[
    [Literal["issue_only", "reposcope"], BenchmarkCase, Path | None],
    Awaitable[BenchmarkResult],
]


async def run_paired_predictions(
    *,
    cases: tuple[BenchmarkCase, ...],
    dataset_digest: str,
    snapshots_root: Path,
    execute: CaseExecutor,
) -> tuple[tuple[BenchmarkResult, ...], tuple[BenchmarkResult, ...]]:
    if len(cases) != 6 or any(case.split != "development" for case in cases):
        raise ValueError("paired prediction requires exactly six development cases")
    issue_only: list[BenchmarkResult] = []
    reposcope: list[BenchmarkResult] = []
    for case in cases:
        issue_result = await execute("issue_only", case, None)
        _validate_single_result(issue_result, case, "issue_only", dataset_digest)
        if issue_result.status != "ok":
            raise EvaluationRunAbort("Issue-only prediction failed safely.")
        repo_result = await execute(
            "reposcope", case, snapshots_root / case.case_id
        )
        _validate_single_result(repo_result, case, "reposcope", dataset_digest)
        if repo_result.status != "ok":
            raise EvaluationRunAbort("RepoScope prediction failed safely.")
        issue_only.append(issue_result)
        reposcope.append(repo_result)
    return tuple(issue_only), tuple(reposcope)


async def preflight_structured_models(model: ModelGateway, *, retries: int = 2) -> None:
    synthetic = {
        "instruction": "Return a minimal schema-valid synthetic preflight value.",
        "issue": {
            "number": 1,
            "title": "Synthetic compatibility preflight",
            "body": "No real repository or benchmark answer is present.",
        },
    }
    requests = (
        (
            ModelPhase.ISSUE_UNDERSTANDING,
            IssueUnderstanding,
            IssueUnderstanding(
                summary="Synthetic issue.",
                observed_behavior="Observed.",
                expected_behavior="Expected.",
            ),
        ),
        (ModelPhase.TOOL_SELECTION, ToolRequest, ToolRequest(complete=True)),
        (
            ModelPhase.EVIDENCE_CRITIQUE,
            CritiqueResult,
            CritiqueResult(sufficient=False),
        ),
        (
            ModelPhase.REPORT_COMPOSITION,
            AnalysisReport,
            AnalysisReport(
                outcome="insufficient_evidence",
                issue_summary="Synthetic issue.",
                observed_behavior="Observed.",
                expected_behavior="Expected.",
                uncertainties=("Synthetic preflight has no source evidence.",),
                confidence=0,
            ),
        ),
        (
            ModelPhase.ISSUE_ONLY_PREDICTION,
            IssueOnlyModelOutput,
            IssueOnlyModelOutput(report_outcome="insufficient_evidence"),
        ),
    )
    for phase, response_model, example in requests:
        await invoke_structured(
            model,
            phase=phase,
            response_model=response_model,
            context={**synthetic, "required_minimal_value": example.model_dump(mode="json")},
            retries=retries,
        )


def _validate_single_result(
    result: BenchmarkResult,
    case: BenchmarkCase,
    system: Literal["issue_only", "reposcope"],
    digest: str,
) -> None:
    if (
        result.case_id != case.case_id
        or result.system != system
        or result.dataset_digest != digest
    ):
        raise ValueError("prediction result does not match its locked case and system")


def write_prediction_artifacts(
    *,
    output_directory: Path,
    run_id: str,
    configuration: DeepSeekRunConfig,
    dataset_digest: str,
    issue_only_results: tuple[BenchmarkResult, ...],
    reposcope_results: tuple[BenchmarkResult, ...],
    call_usage: tuple[ProviderCallUsage, ...],
    reproduction_commands: tuple[str, ...],
    reposcope_commit: str,
    dependency_versions: tuple[DependencyVersion, ...],
    execution_order: tuple[str, ...],
    started_at: datetime,
    finished_at: datetime,
    diagnostics_required: bool = False,
) -> RunArtifactManifest:
    validate_complete_prediction_evidence(
        configuration=configuration,
        dataset_digest=dataset_digest,
        issue_only=issue_only_results,
        reposcope=reposcope_results,
        call_usage=call_usage,
        started_at=started_at,
        finished_at=finished_at,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    config_path = output_directory / "run-config.json"
    issue_path = output_directory / "issue-only.results.v1.jsonl"
    reposcope_path = output_directory / "reposcope.results.v1.jsonl"
    usage_path = output_directory / "call-usage.v1.jsonl"
    reproduce_path = output_directory / "reproduce.txt"
    _write_json(config_path, configuration.model_dump(mode="json"))
    write_jsonl(issue_path, issue_only_results)
    write_jsonl(reposcope_path, reposcope_results)
    write_jsonl(usage_path, call_usage)
    _write_bytes(
        reproduce_path,
        ("\n".join(reproduction_commands).rstrip() + "\n").encode("utf-8"),
    )
    paths = (config_path, issue_path, reposcope_path, usage_path, reproduce_path)
    diagnostic_path = output_directory / DIAGNOSTIC_FILENAME
    if diagnostics_required or diagnostic_path.exists():
        validate_diagnostic_artifact(diagnostic_path, results=reposcope_results, dataset_digest=dataset_digest)
        paths += (diagnostic_path,)
    manifest = RunArtifactManifest(
        schema_version="reposcope.eval.manifest.v1",
        completion_status="complete",
        run_id=run_id,
        dataset_digest=dataset_digest,
        configuration_digest=configuration_digest(configuration),
        reposcope_commit=reposcope_commit,
        working_tree_clean=True,
        python_version=platform.python_version(),
        dependency_versions=dependency_versions,
        execution_order=execution_order,
        started_at=started_at,
        finished_at=finished_at,
        systems=("issue_only", "reposcope"),
        case_count=6,
        artifacts=tuple(
            ArtifactDigest(
                path=path.name,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in paths
        ),
    )
    _write_json(
        output_directory / "prediction-manifest.v1.json",
        manifest.model_dump(mode="json"),
    )
    return manifest


def _validate_result_pair(
    issue_only: tuple[BenchmarkResult, ...],
    reposcope: tuple[BenchmarkResult, ...],
    digest: str,
) -> None:
    if len(issue_only) != 6 or len(reposcope) != 6:
        raise ValueError("prediction artifacts require six results per system")
    issue_ids = {item.case_id for item in issue_only}
    reposcope_ids = {item.case_id for item in reposcope}
    if len(issue_ids) != 6 or issue_ids != reposcope_ids:
        raise ValueError("both systems must cover the same six development cases")
    if any(item.system != "issue_only" for item in issue_only) or any(
        item.system != "reposcope" for item in reposcope
    ):
        raise ValueError("result files must contain their declared system only")
    if any(item.dataset_digest != digest for item in (*issue_only, *reposcope)):
        raise ValueError("all results must use the locked dataset digest")


def _read_digest(path: Path) -> str:
    data = path.read_bytes()
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("dataset digest file must contain one SHA-256 line") from exc
    lines = text.splitlines()
    if len(lines) != 1 or not data.endswith(b"\n"):
        raise ValueError("dataset digest file must contain one SHA-256 line")
    value = lines[0]
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("dataset digest file must contain one SHA-256 line")
    return value


def _write_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    _write_bytes(path, payload)


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _RealCaseExecutor:
    def __init__(
        self,
        *,
        credentials: object,
        configuration: DeepSeekRunConfig,
        ledger: object,
        github: object,
        dataset_digest: str,
        diagnostics: DiagnosticJournal | None = None,
    ) -> None:
        self._credentials = credentials
        self._configuration = configuration
        self._ledger = ledger
        self._github = github
        self._dataset_digest = dataset_digest
        self._diagnostics = diagnostics

    async def __call__(
        self,
        system: Literal["issue_only", "reposcope"],
        case: BenchmarkCase,
        snapshot: Path | None,
    ) -> BenchmarkResult:
        from .deepseek_provider import DeepSeekEvaluationGateway
        from .predictors import RealIssueOnlyPredictor, RealRepoScopeAnalyzer
        from .runners import IssueOnlyRunner, RepoScopeRunner

        gateway = DeepSeekEvaluationGateway(
            credentials=self._credentials,
            configuration=self._configuration,
            ledger=self._ledger,
            case_id=case.case_id,
            system=system,
        )
        if system == "issue_only":
            predictor = RealIssueOnlyPredictor(
                model=gateway,
                configuration=self._configuration,
                ledger=self._ledger,
            )
            return await IssueOnlyRunner(
                predictor,
                runner_id=self._configuration.runner_version,
            ).run(case, dataset_digest=self._dataset_digest)
        if snapshot is None:
            raise ValueError("RepoScope prediction requires its verified snapshot")
        analyzer = RealRepoScopeAnalyzer(
            model=gateway,
            configuration=self._configuration,
            ledger=self._ledger,
            github=self._github,
            diagnostics=self._diagnostics,
        )
        return await RepoScopeRunner(
            analyzer,
            runner_id=self._configuration.runner_version,
        ).run(
            case,
            snapshot_source=snapshot,
            dataset_digest=self._dataset_digest,
        )


async def _run_online(args: argparse.Namespace) -> RunArtifactManifest:
    cases_path = Path(args.cases)
    digest_path = Path(args.dataset_digest_file)
    snapshots_root = Path(args.snapshots_root)
    output_directory = Path(args.output_directory)
    cases, digest = load_development_inputs(
        cases_path=cases_path,
        digest_path=digest_path,
        snapshots_root=snapshots_root,
    )
    configuration = DeepSeekRunConfig.approved()
    if output_directory.exists():
        raise ValueError("output directory must not already exist")
    reposcope_commit, dependency_versions = _source_provenance(Path.cwd())
    diagnostics = DiagnosticJournal(output_directory / DIAGNOSTIC_FILENAME, dataset_digest=digest)
    from app.ingestion import GithubClient

    from .deepseek_provider import (
        DeepSeekCredentials,
        DeepSeekEvaluationGateway,
        InMemoryCallLedger,
    )

    credentials = DeepSeekCredentials(_env_file=None)
    ledger = InMemoryCallLedger(
        max_total_tokens=configuration.max_total_tokens,
        journal_path=output_directory / "call-usage.v1.jsonl",
    )
    started_at = datetime.now(UTC)
    preflight_gateway = DeepSeekEvaluationGateway(
        credentials=credentials,
        configuration=configuration,
        ledger=ledger,
        case_id="schema-preflight",
        system="preflight",
    )
    await preflight_structured_models(
        preflight_gateway, retries=configuration.model_retries
    )
    github_token = os.environ.get("GITHUB_TOKEN")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as http:
        executor = _RealCaseExecutor(
            credentials=credentials,
            configuration=configuration,
            ledger=ledger,
            github=GithubClient(http, token=github_token),
            dataset_digest=digest,
            diagnostics=diagnostics,
        )
        issue_only, reposcope = await run_paired_predictions(
            cases=cases,
            dataset_digest=digest,
            snapshots_root=snapshots_root,
            execute=executor,
        )
    now = datetime.now(UTC)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-deepseek-v4-flash"
    prediction_command = _powershell_command(
        r".\.venv\Scripts\python.exe",
        "-m",
        "app.evaluation.online_run",
        "--split",
        "development",
        "--cases",
        str(cases_path),
        "--dataset-digest-file",
        str(digest_path),
        "--snapshots-root",
        str(snapshots_root),
        "--output-directory",
        str(output_directory),
    )
    scoring_command = _powershell_command(
        r".\.venv\Scripts\python.exe",
        "-m",
        "app.evaluation.development_score",
        "--cases",
        str(cases_path),
        "--dataset-digest-file",
        str(digest_path),
        "--snapshots-root",
        str(snapshots_root),
        "--prediction-directory",
        str(output_directory),
        "--development-gold",
        "evals/development-gold.v1.jsonl",
    )
    return write_prediction_artifacts(
        output_directory=output_directory,
        run_id=run_id,
        configuration=configuration,
        dataset_digest=digest,
        issue_only_results=issue_only,
        reposcope_results=reposcope,
        call_usage=ledger.records,
        reproduction_commands=(prediction_command, scoring_command),
        reposcope_commit=reposcope_commit,
        dependency_versions=dependency_versions,
        execution_order=tuple(
            f"{case.case_id}:{system}"
            for case in cases
            for system in ("issue_only", "reposcope")
        ),
        started_at=started_at,
        finished_at=now,
        diagnostics_required=True,
    )


def _powershell_command(*arguments: str) -> str:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return "& " + " ".join(quote(argument) for argument in arguments)


def _source_provenance(root: Path) -> tuple[str, tuple[DependencyVersion, ...]]:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("source tree status is unavailable") from exc
    if status.stdout:
        raise ValueError("real evaluation requires a clean source tree")
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("source revision is unavailable") from exc
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("source revision is unavailable")
    dependencies = tuple(
        DependencyVersion(name=name, version=importlib.metadata.version(name))
        for name in REQUIRED_DEPENDENCIES
    )
    return revision, dependencies


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cases_parent = Path(args.cases).resolve(strict=False).parent
    install_gold_read_guard(
        (
            cases_parent / "development-gold.v1.jsonl",
            Path("local/evaluation/hidden-gold.v1.jsonl"),
        )
    )
    try:
        manifest = asyncio.run(_run_online(args))
    except ValidationError:
        print(
            "evaluation failed safely: credentials_not_configured",
            file=sys.stderr,
        )
        return 2
    except EvaluationRunAbort:
        print("evaluation failed safely: run_safety_abort", file=sys.stderr)
        return 2
    except ModelGatewayError:
        print("evaluation failed safely: provider_preflight_failed", file=sys.stderr)
        return 2
    except ValueError:
        print("evaluation failed safely: input_validation_failed", file=sys.stderr)
        return 2
    except OSError:
        print("evaluation failed safely: artifact_io_failed", file=sys.stderr)
        return 2
    print(
        f"wrote complete six-case paired predictions for run {manifest.run_id}; "
        "gold was unavailable and no score was computed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
