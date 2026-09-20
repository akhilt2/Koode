"""SQLite persistence for Koode's users and clinical symptom events."""

from datetime import datetime, timezone
import os

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./koode.db")

# SQLite needs this flag when a request is handled by a different worker thread.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def utc_now() -> datetime:
    """Return a timezone-aware timestamp consistently across the application."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    """A WhatsApp participant, identified by their Twilio phone number."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    logs: Mapped[list["SymptomLog"]] = relationship(back_populates="user")


class SymptomLog(Base):
    """One AI-translated event received through the WhatsApp webhook."""

    __tablename__ = "symptom_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    original_message: Mapped[str] = mapped_column(Text)
    user_type: Mapped[str] = mapped_column(String(20), index=True)
    translated_english_summary: Mapped[str] = mapped_column(Text)
    symptoms: Mapped[str] = mapped_column(Text, default="[]")
    severity_level: Mapped[str] = mapped_column(String(20), index=True)
    medications_given: Mapped[str] = mapped_column(Text, default="")
    whatsapp_reply: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    user: Mapped[User] = relationship(back_populates="logs")


def init_db() -> None:
    """Create tables on startup; SQLite does not require migrations for the demo."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency that always closes its short-lived DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
