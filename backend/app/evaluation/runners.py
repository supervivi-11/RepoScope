from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from .contracts import (
    BenchmarkCase,
    BenchmarkResult,
    EvaluationModel,
    EvaluationPrediction,
)
from .snapshot import snapshot_tree_digest


class IssueOnlyInput(EvaluationModel):
    repo_url: str
    issue_number: int
    issue_title: str
    issue_body: str | None = None


IssueOnlyPredictor = Callable[[IssueOnlyInput], Awaitable[EvaluationPrediction]]
RepoScopeAnalyzer = Callable[[BenchmarkCase, Path], Awaitable[EvaluationPrediction]]


class IssueOnlyRunner:
    def __init__(self, predictor: IssueOnlyPredictor, *, runner_id: str) -> None:
        self._predictor = predictor
        self._runner_id = runner_id

    async def run(self, case: BenchmarkCase, *, dataset_digest: str) -> BenchmarkResult:
        try:
            prediction = await self._predictor(
                IssueOnlyInput(
                    repo_url=case.repo_url,
                    issue_number=case.issue_number,
                    issue_title=case.issue_title,
                    issue_body=case.issue_body,
                )
            )
        except Exception:
            return _failed(case, "issue_only", self._runner_id, dataset_digest)
        return _result(case, "issue_only", prediction, self._runner_id, dataset_digest)


class RepoScopeRunner:
    def __init__(self, analyzer: RepoScopeAnalyzer, *, runner_id: str) -> None:
        self._analyzer = analyzer
        self._runner_id = runner_id

    async def run(
        self,
        case: BenchmarkCase,
        *,
        snapshot_source: Path,
        dataset_digest: str,
    ) -> BenchmarkResult:
        source = snapshot_source.resolve(strict=True)
        if snapshot_source.is_symlink() or not source.is_dir():
            raise ValueError("snapshot source must be a real directory")
        if snapshot_tree_digest(source) != case.snapshot_tree_digest:
            raise ValueError("snapshot digest does not match the frozen benchmark case")
        with tempfile.TemporaryDirectory(prefix="reposcope-eval-") as directory:
            owned_root = Path(directory).resolve()
            target = owned_root / "snapshot"
            shutil.copytree(source, target, symlinks=False)
            if snapshot_tree_digest(target) != case.snapshot_tree_digest:
                raise ValueError("copied snapshot digest changed")
            try:
                prediction = await self._analyzer(case, target)
            except Exception:
                return _failed(case, "reposcope", self._runner_id, dataset_digest)
        return _result(case, "reposcope", prediction, self._runner_id, dataset_digest)


def _result(
    case: BenchmarkCase,
    system: str,
    prediction: EvaluationPrediction,
    runner_id: str,
    dataset_digest: str,
) -> BenchmarkResult:
    payload = EvaluationPrediction.model_validate(
        prediction.model_dump(exclude={"schema_version", "case_id", "system"})
    ).model_dump()
    return BenchmarkResult(
        **payload,
        schema_version="reposcope.eval.result.v1",
        case_id=case.case_id,
        system=system,
        status="ok",
        runner_id=runner_id,
        dataset_digest=dataset_digest,
        safe_error_code=None,
    )


def _failed(
    case: BenchmarkCase,
    system: str,
    runner_id: str,
    dataset_digest: str,
) -> BenchmarkResult:
    return BenchmarkResult(
        schema_version="reposcope.eval.result.v1",
        case_id=case.case_id,
        system=system,
        status="failed",
        runner_id=runner_id,
        dataset_digest=dataset_digest,
        safe_error_code="runner_failure",
    )
