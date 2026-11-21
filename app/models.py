"""Database schema for stage 1 (PostgreSQL; can be migrated in later stages)."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram ID
    username: Mapped[str | None] = mapped_column(String(100))
    full_name: Mapped[str | None] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    intro_queued: Mapped[bool] = mapped_column(Boolean, default=False)


class UserRole(Base):
    __tablename__ = 'user_roles'
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'), primary_key=True)
    role: Mapped[str] = mapped_column(String(40), primary_key=True)


class Registration(Base):
    __tablename__ = 'registrations'
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'), primary_key=True)
    day: Mapped[int] = mapped_column(Integer, primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PointEntry(Base):
    __tablename__ = 'point_entries'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'), index=True)
    delta: Mapped[Decimal] = mapped_column(Numeric(9, 1))
    source_key: Mapped[str] = mapped_column(String(180), unique=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Submission(Base):
    __tablename__ = 'submissions'
    __table_args__ = (UniqueConstraint('user_id', 'task_id', name='uq_submission_user_task'),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'), index=True)
    task_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default='draft')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parts: Mapped[list[SubmissionPart]] = relationship(back_populates='submission', cascade='all, delete-orphan')


class SubmissionPart(Base):
    __tablename__ = 'submission_parts'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('submissions.id'), index=True)
    content_type: Mapped[str] = mapped_column(String(20))
    text: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(Text)
    submission: Mapped[Submission] = relationship(back_populates='parts')


class PlannedMessage(Base):
    __tablename__ = 'planned_messages'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint('user_id', 'kind', name='uq_planned_user_kind'),)


class BotSetting(Base):
    """Settings that are changed inside the bot without editing secrets or restarting."""
    __tablename__ = 'bot_settings'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class SupportCase(Base):
    """One case per conversation; next participant message after close opens a new case."""
    __tablename__ = 'support_cases'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'), index=True)
    status: Mapped[str] = mapped_column(String(24), default='open')
    group_root_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupportMirror(Base):
    """Maps bot messages in the support group back to a participant's case."""
    __tablename__ = 'support_mirrors'
    group_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    case_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('support_cases.id'), index=True)


class TripAttendee(Base):
    """Organizer-supplied participant allowlist. One full name can be claimed by one Telegram account."""
    __tablename__ = 'trip_attendees'
    normalized_name: Mapped[str] = mapped_column(String(180), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(180), nullable=False)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('users.id'), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UrgentBroadcast(Base):
    """Audit trail for urgent messages sent by authorized staff."""
    __tablename__ = 'urgent_broadcasts'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default='sending')
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
