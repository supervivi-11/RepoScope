from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base used by application models and Alembic."""

    metadata = MetaData()


metadata = Base.metadata
