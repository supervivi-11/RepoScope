from pathlib import Path

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
