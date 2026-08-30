from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.analysis import AnalysisRepository
from app.api import create_app
from app.config import Settings

settings = Settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
sessions = async_sessionmaker(engine, expire_on_commit=False)
repository = AnalysisRepository(sessions)


async def database_probe() -> bool:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return True


app = create_app(repository=repository, database_probe=database_probe)
