from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.snapshot_paths import validate_snapshot_root


class Settings(BaseSettings):
    """Runtime configuration sourced from the process environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://reposcope:reposcope@localhost:5432/reposcope"
    )
    github_token: SecretStr | None = None
    snapshot_root: Path = Path(".reposcope/snapshots")
    worker_id: str = f"worker-{uuid4()}"
    worker_max_attempts: int = 3
    snapshot_janitor_interval_seconds: float = 3600

    @field_validator("snapshot_root", mode="before")
    @classmethod
    def _validate_snapshot_root(cls, value: object) -> Path:
        return validate_snapshot_root(value)
