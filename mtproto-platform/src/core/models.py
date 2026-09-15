from __future__ import annotations
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, ForeignKey, Float, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship, DeclarativeBase
from sqlalchemy.dialects.postgresql import UUID

def utc_now() -> datetime:
    """Helper to consistently generate aware UTC datetimes."""
    return datetime.now(timezone.utc)

class Base(DeclarativeBase):
    pass

class Proxy(Base):
    __tablename__ = "proxy"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    secret: Mapped[str] = mapped_column(String, nullable=False)
    protocol: Mapped[str] = mapped_column(String, nullable=False, default="mtproto")
    fingerprint: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # ارتباطات با رفتار Cascade مناسب
    discoveries: Mapped[list["ProxyDiscovery"]] = relationship(back_populates="proxy", cascade="all, delete-orphan")
    observations: Mapped[list["ProxyObservation"]] = relationship(back_populates="proxy", cascade="all, delete-orphan")
    score: Mapped["ProxyScore"] = relationship(back_populates="proxy", cascade="all, delete-orphan", uselist=False)

    __table_args__ = (
        Index("ix_proxy_server_port", "server", "port"),
        Index("ix_proxy_last_tested_at", "last_tested_at"),
        Index("ix_proxy_protocol", "protocol"),
    )


class ProxySource(Base):
    __tablename__ = "proxy_source"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    
    discoveries: Mapped[list["ProxyDiscovery"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class ProxyDiscovery(Base):
    __tablename__ = "proxy_discovery"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proxy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proxy.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proxy_source.id", ondelete="CASCADE"), nullable=False)
    
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    
    proxy: Mapped["Proxy"] = relationship(back_populates="discoveries")
    source: Mapped["ProxySource"] = relationship(back_populates="discoveries")

    __table_args__ = (
        Index("ix_proxy_discovery_proxy_id", "proxy_id"),
        Index("ix_proxy_discovery_source_id", "source_id"),
        Index("ix_proxy_discovery_discovered_at", "discovered_at"),
    )


class ProxyObservation(Base):
    __tablename__ = "proxy_observation"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proxy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proxy.id", ondelete="CASCADE"), nullable=False)
    
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    is_success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    
    tcp_latency_ms: Mapped[float] = mapped_column(Float, nullable=True)
    e2e_latency_ms: Mapped[float] = mapped_column(Float, nullable=True)
    
    error_category: Mapped[str] = mapped_column(String, nullable=True)
    error_message: Mapped[str] = mapped_column(String, nullable=True)
    test_location: Mapped[str] = mapped_column(String, nullable=True)
    
    proxy: Mapped["Proxy"] = relationship(back_populates="observations")

    __table_args__ = (
        Index("ix_proxy_obs_proxy_id_timestamp", "proxy_id", "timestamp"),
        Index("ix_proxy_obs_timestamp", "timestamp"),
        Index("ix_proxy_obs_is_success", "is_success"),
    )


class ProxyScore(Base):
    __tablename__ = "proxy_score"
    
    # One-to-One با جدول Proxy. خود proxy_id کلید اصلی هم هست.
    proxy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proxy.id", ondelete="CASCADE"), primary_key=True)
    
    reliability_24h_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_e2e_latency_ms: Mapped[float] = mapped_column(Float, nullable=True)
    successful_tests_24h: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_tests_24h: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tests_24h: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    
    last_calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    
    proxy: Mapped["Proxy"] = relationship(back_populates="score")