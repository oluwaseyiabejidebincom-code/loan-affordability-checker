import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class AnalysisSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: str
    account_name: str | None
    risk_grade: str | None
    created_at: datetime


class AnalysisDetail(AnalysisSummary):
    error: str | None
    bank: str | None
    account_number_masked: str | None
    period_start: date | None
    period_end: date | None
    result: dict | None
    report: str | None


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: date
    narration: str
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal
    category: str
