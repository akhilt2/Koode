"""SQLite persistence for Koode's users and clinical symptom events."""

from datetime import datetime, timezone
import os
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

load_dotenv()


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
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    user: Mapped[User] = relationship(back_populates="logs")


def init_db() -> None:
    """Create tables and add the resolved flag to existing hackathon databases."""
    Base.metadata.create_all(bind=engine)
    if "symptom_logs" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("symptom_logs")}
        if "resolved" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE symptom_logs ADD COLUMN resolved BOOLEAN NOT NULL DEFAULT 0"))


def get_db():
    """FastAPI dependency that always closes its short-lived DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_logs_by_date_range(
    db,
    start_date: datetime,
    end_date: datetime,
    user_id: Optional[int] = None,
    include_resolved: bool = False,
) -> list[SymptomLog]:
    """Return chronological logs in a half-open UTC date range.

    Keeping this query in the data layer lets the dashboard, AI report endpoint,
    and printable report share exactly the same filtering behavior.
    """
    statement = select(SymptomLog).where(
        SymptomLog.created_at >= start_date,
        SymptomLog.created_at <= end_date,
    )
    if user_id is not None:
        statement = statement.where(SymptomLog.user_id == user_id)
    if not include_resolved:
        statement = statement.where(SymptomLog.resolved.is_(False))
    return list(db.scalars(statement.order_by(SymptomLog.created_at.asc())).all())
