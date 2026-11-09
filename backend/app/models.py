import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


Money = Numeric(16, 2)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued, processing, completed, failed
    error: Mapped[str | None] = mapped_column(Text)

    bank: Mapped[str | None] = mapped_column(Text)
    account_name: Mapped[str | None] = mapped_column(Text)
    account_number_masked: Mapped[str | None] = mapped_column(String(20))
    period_start: Mapped[date | None]
    period_end: Mapped[date | None]
    risk_grade: Mapped[str | None] = mapped_column(String(20))
    result: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))  # metrics, flags, tamper signs
    report: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    date: Mapped[date]
    narration: Mapped[str] = mapped_column(Text)
    debit: Mapped[Decimal | None] = mapped_column(Money)
    credit: Mapped[Decimal | None] = mapped_column(Money)
    balance: Mapped[Decimal] = mapped_column(Money)
    category: Mapped[str] = mapped_column(String(30))
