import pytest
from datetime import timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.config import get_settings
from src.core.models import Base, Proxy, ProxySource, ProxyDiscovery, ProxyObservation, ProxyScore

@pytest.fixture
async def test_engine():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        pytest.skip(f"Database not available: {e}")
        
    yield engine
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

@pytest.fixture
async def db_session(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()

@pytest.mark.asyncio
async def test_db_connection_and_proxy_insert(db_session) -> None:
    proxy = Proxy(server="1.2.3.4", port=443, secret="ee0", fingerprint="hash_1")
    db_session.add(proxy)
    await db_session.commit()
    assert proxy.id is not None
    assert proxy.created_at.tzinfo == timezone.utc

@pytest.mark.asyncio
async def test_duplicate_fingerprint_rejected(db_session) -> None:
    p1 = Proxy(server="1.1.1.1", port=443, secret="ee1", fingerprint="hash_dup")
    p2 = Proxy(server="1.1.1.2", port=443, secret="ee2", fingerprint="hash_dup")
    db_session.add(p1)
    await db_session.commit()
    
    db_session.add(p2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

@pytest.mark.asyncio
async def test_foreign_keys_and_relationships(db_session) -> None:
    proxy = Proxy(server="2.2.2.2", port=443, secret="ee", fingerprint="rel_hash")
    source = ProxySource(name="Source 1", url="http://t", source_type="api")
    db_session.add_all([proxy, source])
    await db_session.commit()
    
    discovery = ProxyDiscovery(proxy_id=proxy.id, source_id=source.id)
    observation = ProxyObservation(proxy_id=proxy.id, is_success=True, tcp_latency_ms=40.5)
    score = ProxyScore(proxy_id=proxy.id, reliability_24h_pct=100.0)
    db_session.add_all([discovery, observation, score])
    await db_session.commit()
    
    assert discovery.id is not None
    assert score.proxy_id == proxy.id

@pytest.mark.asyncio
async def test_multiple_observations_preserved(db_session) -> None:
    proxy = Proxy(server="3.3.3.3", port=443, secret="ee", fingerprint="obs_hash")
    db_session.add(proxy)
    await db_session.commit()
    
    obs1 = ProxyObservation(proxy_id=proxy.id, is_success=False)
    obs2 = ProxyObservation(proxy_id=proxy.id, is_success=True)
    db_session.add_all([obs1, obs2])
    await db_session.commit()
    
    result = await db_session.execute(select(ProxyObservation).where(ProxyObservation.proxy_id == proxy.id))
    obs = result.scalars().all()
    assert len(obs) == 2