"""Alembic environment placeholder for RepoScope persistence migrations."""

from alembic import context


def run_migrations_online() -> None:
    """Configure database-backed migrations when persistence is introduced."""
    raise RuntimeError("No database migrations are defined in the project foundation.")


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not configured in the project foundation.")
