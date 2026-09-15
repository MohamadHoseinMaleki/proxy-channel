from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.core.config import get_settings

settings = get_settings()

# ساخت Engine آسنکرون برای اتصال به Postgres
engine = create_async_engine(
    settings.database_url,
    echo=(settings.env == "dev"),
    pool_pre_ping=True,
)

# ساخت Session ساز
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)