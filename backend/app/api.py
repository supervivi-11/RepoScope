from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, AsyncContextManager, Literal
from uuid import UUID

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent import AnalysisReport
from app.ingestion import RepositoryCoordinates, validate_issue_number
from app.report_limits import PREVIOUS_REPORT_HISTORY_MAX

from .analysis import (
    AnalysisConflictError,
    AnalysisNotFoundError,
    AnalysisRepository,
    PersistentAnalysisStatus,
    StoredAnalysis,
    StoredEvent,
    TERMINAL_STATUSES,
)
from .analysis.domain import validate_event_type


_MAX_REQUEST_BYTES = 16_384
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9._~:/+\-]{1,200}$"


@dataclass(slots=True)
class _BoundedBodyBuffer:
    limit: int
    _received: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("request body limit cannot be negative")

    def append(self, chunk: bytes) -> bool:
        remaining = self.limit - len(self._received)
        if remaining > 0:
            self._received.extend(chunk[:remaining])
        return len(chunk) <= remaining

    @property
    def data(self) -> bytes:
        return bytes(self._received)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(ApiModel):
    status: Literal["ok"]
    service: Literal["reposcope-api"]


class ReadinessResponse(ApiModel):
    status: Literal["ready"]
    database: Literal["ok"]


class ErrorBody(ApiModel):
    code: str
    message: str


class ErrorResponse(ApiModel):
    error: ErrorBody


class CreateAnalysisRequest(ApiModel):
    repo_url: str = Field(min_length=1, max_length=300)
    issue_number: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def _validate_domain_input(self) -> CreateAnalysisRequest:
        coordinates = RepositoryCoordinates.parse(self.repo_url)
        validate_issue_number(self.issue_number)
        self.repo_url = coordinates.canonical_url
        return self


class CreateAnalysisResponse(ApiModel):
    analysis_id: UUID
    status: PersistentAnalysisStatus


class PublicAnalysisError(ApiModel):
    code: str
    message: str


class ReportVersionResponse(ApiModel):
    version: int
    report: AnalysisReport
    created_at: datetime


class AnalysisResponse(ApiModel):
    analysis_id: UUID
    repo_url: str
    issue_number: int
    status: PersistentAnalysisStatus
    progress: dict[str, Any]
    counters: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    error: PublicAnalysisError | None
    current_report: AnalysisReport | None
    report_history: tuple[ReportVersionResponse, ...] = Field(
        max_length=PREVIOUS_REPORT_HISTORY_MAX
    )


class FeedbackRequest(ApiModel):
    action: Literal["accept", "revise"]
    comment: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def _validate_comment(self) -> FeedbackRequest:
        if self.action == "revise":
            if self.comment is None or not self.comment.strip():
                raise ValueError("revision comment must be nonblank")
        elif self.comment is not None:
            raise ValueError("accept feedback cannot include a comment")
        return self


class FeedbackResponse(ApiModel):
    analysis_id: UUID
    status: PersistentAnalysisStatus


class DemoEvent(ApiModel):
    sequence: int = Field(ge=1)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    data: dict[str, Any]


class DemoCase(ApiModel):
    case_id: str
    artifact_version: str
    title: str
    repo_url: str
    issue_number: int
    report: AnalysisReport
    events: tuple[DemoEvent, ...]


class DemoCasesResponse(ApiModel):
    version: str
    cases: tuple[DemoCase, ...]


class StaticDemoStore:
    """Validated committed artifacts; this boundary has no model dependency."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path(__file__).with_name("demo_cases.json")

    def load(self) -> DemoCasesResponse:
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        return DemoCasesResponse.model_validate(payload)


@dataclass(frozen=True, slots=True)
class SSESettings:
    poll_interval: float = 0.5
    heartbeat_interval: float = 15.0

    def __post_init__(self) -> None:
        if self.poll_interval < 0 or self.heartbeat_interval < 0:
            raise ValueError("SSE intervals cannot be negative")


async def _database_unconfigured() -> bool:
    return False


def create_app(
    *,
    repository: AnalysisRepository,
    demo_store: StaticDemoStore | None = None,
    database_probe: Callable[[], Awaitable[bool]] = _database_unconfigured,
    sse_settings: SSESettings | None = None,
    lifespan: Callable[[FastAPI], AsyncContextManager[None]] | None = None,
) -> FastAPI:
    application = FastAPI(title="RepoScope API", lifespan=lifespan)
    demos = demo_store or StaticDemoStore()
    stream_settings = sse_settings or SSESettings()

    @application.middleware("http")
    async def limit_request_size(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > _MAX_REQUEST_BYTES
            except ValueError:
                too_large = True
            if too_large:
                return _error_response(
                    413, "request_too_large", "Request body is too large."
                )
        received = _BoundedBodyBuffer(_MAX_REQUEST_BYTES)
        async for chunk in request.stream():
            if not received.append(chunk):
                return _error_response(
                    413, "request_too_large", "Request body is too large."
                )
        request._body = received.data
        return await call_next(request)

    @application.exception_handler(AnalysisNotFoundError)
    async def not_found_handler(request: Request, error: AnalysisNotFoundError):
        return _error_response(404, "not_found", "Analysis was not found.")

    @application.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, error: RequestValidationError):
        return _error_response(
            422, "validation_error", "Request validation failed."
        )

    @application.exception_handler(AnalysisConflictError)
    async def conflict_handler(request: Request, error: AnalysisConflictError):
        return _error_response(409, "conflict", "Analysis state conflicts with the request.")

    @application.exception_handler(Exception)
    async def internal_handler(request: Request, error: Exception):
        return _error_response(500, "internal_error", "Analysis failed safely.")

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service="reposcope-api")

    @application.get(
        "/ready",
        response_model=ReadinessResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def ready():
        try:
            available = await database_probe()
        except Exception:
            available = False
        if not available:
            return _error_response(
                503, "database_unavailable", "Database is not ready."
            )
        return ReadinessResponse(status="ready", database="ok")

    @application.post(
        "/api/v1/analyses",
        response_model=CreateAnalysisResponse,
        status_code=202,
        responses={200: {"model": CreateAnalysisResponse}, 409: {"model": ErrorResponse}},
    )
    async def create_analysis(
        body: CreateAnalysisRequest,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                max_length=200,
                pattern=_IDEMPOTENCY_PATTERN,
            ),
        ] = None,
    ):
        result = await repository.create_analysis(
            repo_url=body.repo_url,
            issue_number=body.issue_number,
            idempotency_key=idempotency_key,
        )
        response = CreateAnalysisResponse(
            analysis_id=result.analysis_id, status=result.status
        )
        return JSONResponse(
            status_code=200 if result.replayed else 202,
            content=response.model_dump(mode="json"),
        )

    @application.get(
        "/api/v1/analyses/{analysis_id}",
        response_model=AnalysisResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_analysis(analysis_id: UUID) -> AnalysisResponse:
        return _analysis_response(await repository.get_analysis(analysis_id))

    @application.get(
        "/api/v1/analyses/{analysis_id}/events",
        responses={404: {"model": ErrorResponse}},
    )
    async def analysis_events(
        analysis_id: UUID,
        request: Request,
        last_event_id: Annotated[
            str | None,
            Header(alias="Last-Event-ID", max_length=20, pattern=r"^[0-9]+$"),
        ] = None,
    ) -> StreamingResponse:
        await repository.get_analysis(analysis_id)
        after_sequence = int(last_event_id) if last_event_id is not None else 0
        return StreamingResponse(
            iter_sse_events(
                repository,
                analysis_id,
                request=request,
                after_sequence=after_sequence,
                settings=stream_settings,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @application.post(
        "/api/v1/analyses/{analysis_id}/feedback",
        response_model=FeedbackResponse,
        status_code=202,
        responses={200: {"model": FeedbackResponse}, 409: {"model": ErrorResponse}},
    )
    async def feedback(analysis_id: UUID, body: FeedbackRequest):
        result = await repository.submit_feedback(
            analysis_id, action=body.action, comment=body.comment
        )
        response = FeedbackResponse(analysis_id=analysis_id, status=result.status)
        return JSONResponse(
            status_code=200 if result.replayed else 202,
            content=response.model_dump(mode="json"),
        )

    @application.get("/api/v1/demo-cases", response_model=DemoCasesResponse)
    async def demo_cases() -> DemoCasesResponse:
        return demos.load()

    return application


async def iter_sse_events(
    repository: AnalysisRepository,
    analysis_id: UUID,
    *,
    request: Request,
    after_sequence: int,
    settings: SSESettings,
) -> AsyncIterator[str]:
    cursor = after_sequence
    loop = asyncio.get_running_loop()
    last_output = loop.time()
    while True:
        if await request.is_disconnected():
            return
        events, status = await repository.event_stream_snapshot(
            analysis_id, after_sequence=cursor
        )
        for event in events:
            cursor = event.sequence
            last_output = loop.time()
            yield _format_sse(event)
        if status in TERMINAL_STATUSES:
            return
        await asyncio.sleep(settings.poll_interval)
        if loop.time() - last_output >= settings.heartbeat_interval:
            last_output = loop.time()
            yield ": heartbeat\n\n"


def _format_sse(event: StoredEvent) -> str:
    event_type = validate_event_type(event.event_type)
    data = json.dumps(event.data, sort_keys=True, separators=(",", ":"))
    return (
        f"id: {event.sequence}\n"
        f"event: {event_type}\n"
        f"data: {data}\n\n"
    )


def _analysis_response(stored: StoredAnalysis) -> AnalysisResponse:
    error = None
    if stored.error_code is not None and stored.error_message is not None:
        error = PublicAnalysisError(
            code=stored.error_code, message=stored.error_message
        )
    return AnalysisResponse(
        analysis_id=stored.analysis_id,
        repo_url=stored.repo_url,
        issue_number=stored.issue_number,
        status=stored.status,
        progress=stored.progress,
        counters=stored.counters,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        error=error,
        current_report=stored.current_report,
        report_history=tuple(
            ReportVersionResponse(
                version=item.version,
                report=item.report,
                created_at=item.created_at,
            )
            for item in stored.report_history[:-1]
        ),
    )


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(error=ErrorBody(code=code, message=message))
    return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))
