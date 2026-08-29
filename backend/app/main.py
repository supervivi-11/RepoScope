from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="RepoScope API")


class HealthResponse(BaseModel):
    status: str
    service: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="reposcope-api")
