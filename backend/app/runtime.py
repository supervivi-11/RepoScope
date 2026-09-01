from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.agent import OpenAICompatibleGateway, OpenAIModelSettings, build_investigation_graph
from app.analysis import AnalysisRepository, AnalysisWorker, PostgresJobQueue
from app.analysis.checkpoints import PostgresCheckpointFactory
from app.analysis.janitor import SnapshotJanitor
from app.analysis.service import DisabledWorkerService, WorkerService
from app.config import Settings
from app.ingestion import GithubClient, IngestionService, SafeArchiveExtractor, SnapshotCleaner
from app.investigation import InvestigationTools, PythonRepositoryIndex


@dataclass(slots=True)
class WorkerRuntime:
    service: WorkerService | DisabledWorkerService
    janitor: SnapshotJanitor
    engine: AsyncEngine
    http: httpx.AsyncClient

    async def close(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()


def build_worker_runtime(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    http: httpx.AsyncClient | None = None,
    model: Any | None = None,
) -> WorkerRuntime:
    """Production composition root with injectable external boundaries."""
    active = settings or Settings()
    model_settings = None if model is not None else OpenAIModelSettings()
    gateway = model
    if gateway is None and model_settings is not None and model_settings.api_key is not None:
        gateway = OpenAICompatibleGateway.from_env()
    runtime_engine = engine or create_async_engine(active.database_url, pool_pre_ping=True)
    runtime_http = http or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    repository = AnalysisRepository(sessions)
    queue = PostgresJobQueue(sessions)
    github = GithubClient.from_settings(runtime_http, active)
    ingestion = IngestionService.from_settings(
        github, SafeArchiveExtractor(), active
    )
    checkpoints = PostgresCheckpointFactory(active.database_url)
    cleaner = SnapshotCleaner(active.snapshot_root)
    if gateway is None:
        service = DisabledWorkerService()
    else:
        worker = AnalysisWorker(
            repository=repository,
            queue=queue,
            ingestion=ingestion,
            index_builder=PythonRepositoryIndex.build,
            tools_builder=lambda index: InvestigationTools(index, github),
            graph_builder=lambda tools, checkpointer: build_investigation_graph(
                tools=tools,
                model=gateway,
                checkpointer=checkpointer,
            ),
            checkpoint_factory=checkpoints,
            snapshot_cleaner=cleaner,
        )
        service = WorkerService(
            queue=queue,
            worker=worker,
            repository=repository,
            worker_id=active.worker_id,
            max_attempts=active.worker_max_attempts,
        )
    return WorkerRuntime(
        service=service,
        janitor=SnapshotJanitor(
            root=active.snapshot_root,
            repository=repository,
            interval=active.snapshot_janitor_interval_seconds,
        ),
        engine=runtime_engine,
        http=runtime_http,
    )
