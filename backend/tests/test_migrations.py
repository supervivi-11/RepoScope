from alembic import command
from alembic.config import Config

from app.db import metadata


def test_alembic_environment_uses_application_metadata(tmp_path, monkeypatch) -> None:
    """Breaks if Alembic cannot load application metadata from DATABASE_URL."""
    database_url = f"sqlite:///{tmp_path / 'reposcope.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("backend/alembic.ini")

    command.current(config)

    assert metadata.tables == {}
