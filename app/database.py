"""Persist call state and appointment outcomes in SQLite locally or Postgres in deployment."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ServiceRequest(Base):
    __tablename__ = "service_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    call_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[str] = mapped_column(String(20), default="standard")
    booking_type: Mapped[str] = mapped_column(String(30), default="direct")
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="intake")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    offered_slots: Mapped[list] = mapped_column(JSON, default=list)
    booking_state: Mapped[str] = mapped_column(String(20), default="idle")
    booking_operation_id: Mapped[str | None] = mapped_column(String(36))
    followup_reason: Mapped[str | None] = mapped_column(Text)
    call_status: Mapped[str | None] = mapped_column(String(40))
    call_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("service_requests.id"), unique=True)
    cal_uid: Mapped[str] = mapped_column(String(200), unique=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    email_requested: Mapped[bool] = mapped_column(default=False)
    dispatch_hold: Mapped[bool] = mapped_column(default=False)
    booked_facts: Mapped[dict] = mapped_column(JSON, default=dict)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InquiryEmail(Base):
    """One acknowledgment attempt per request; uncertain sends are never repeated blindly."""

    __tablename__ = "inquiry_emails"
    request_id: Mapped[str] = mapped_column(ForeignKey("service_requests.id"), primary_key=True)
    recipient: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(30), default="submitting")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def make_database(url: str):
    """Create an engine and session factory; do not put SQLite on an ephemeral production disk."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite:///./"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, **kwargs)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def load_request(session, call_id: str) -> ServiceRequest | None:
    """Find the request tied to the authenticated Retell call."""
    return session.scalar(select(ServiceRequest).where(ServiceRequest.call_id == call_id))
