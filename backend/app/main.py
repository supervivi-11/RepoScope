from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.analysis import AnalysisRepository
from app.api import create_app
from app.config import Settings
from app.readiness import database_schema_ready

settings = Settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
sessions = async_sessionmaker(engine, expire_on_commit=False)
repository = AnalysisRepository(sessions)


async def database_probe() -> bool:
    return await database_schema_ready(engine)


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        await engine.dispose()


app = create_app(
    repository=repository,
    database_probe=database_probe,
    lifespan=lifespan,
)
