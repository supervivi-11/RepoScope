from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
