import asyncio
import io
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import openai
import pdfplumber
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, update

from app.agent.graph import DocumentRejected, ModelUnavailable, pipeline
from app.api_models import AnalysisDetail, AnalysisSummary, TransactionOut
from app.db import Session, init_db
from app.models import Analysis, Transaction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="LoanLens", lifespan=lifespan)


MAX_PAGES = 15  # the whole statement goes to the model in one call


class ParsedPdf(BaseModel):
    pages_text: str
    made_by: str


def read_pdf(pdf_bytes: bytes) -> ParsedPdf:
    if not pdf_bytes.startswith(b"%PDF"):
        raise DocumentRejected("Only PDF statements are accepted.")
    try:
        opened_file = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception:
        raise DocumentRejected("The PDF is damaged or password-protected. Upload an unlocked copy.") from None
    with opened_file:
        if len(opened_file.pages) > MAX_PAGES:
            raise DocumentRejected(f"The statement has more than {MAX_PAGES} pages.")
        # layout=True keeps the debit and credit columns apart
        pages_text = "\n\n".join(page.extract_text(layout=True, x_density=4) or "" for page in opened_file.pages)
        info = opened_file.metadata or {}
        made_by = f"{info.get('Producer', '')} {info.get('Creator', '')}".strip()
    if len(pages_text.strip()) < 200:  # scanned PDFs have no text layer
        raise DocumentRejected("No text found. Upload the PDF downloaded from the bank's app, not a scan or photo.")
    return ParsedPdf(pages_text=pages_text, made_by=made_by)


@app.post("/analyses", status_code=202, response_model=AnalysisSummary)
async def create_analysis(
    file: UploadFile,
    background: BackgroundTasks,
    loan_amount: float | None = Form(None, gt=0),
    loan_months: int | None = Form(None, ge=1, le=120),
    interest_rate: float = Form(0, ge=0, le=200),
) -> Analysis:
    """Upload a statement PDF, optionally with the requested loan. It is analysed in the background;
    poll GET /analyses/{id} for the result."""
    if (loan_amount is None) != (loan_months is None):
        raise HTTPException(422, "Give both the loan amount and the term, or neither.")
    loan = {"amount": loan_amount, "months": loan_months, "rate": interest_rate} if loan_amount else None
    pdf_bytes = await file.read()
    try:
        parsed_pdf = await asyncio.to_thread(read_pdf, pdf_bytes)
    except DocumentRejected as e:
        raise HTTPException(422, str(e)) from None
    async with Session() as db:
        analysis_row = Analysis(filename=(file.filename or "statement.pdf")[:255])
        db.add(analysis_row)
        await db.commit()
    background.add_task(run_agent, analysis_row.id, parsed_pdf, loan)
    return analysis_row


async def run_agent(analysis_id: uuid.UUID, parsed_pdf: ParsedPdf, loan: dict | None) -> None:
    await set_status(analysis_id, status="processing")
    try:
        final_state = await pipeline.ainvoke(
            {"text": parsed_pdf.pages_text, "producer": parsed_pdf.made_by, "loan": loan}
        )
        await save_result(analysis_id, final_state)
        return
    except (DocumentRejected, ModelUnavailable) as e:
        error = str(e)
    except openai.AuthenticationError:
        error = "OpenAI rejected the API key in backend/.env. Check it and restart the API."
    except Exception:
        log.exception("Analysis %s failed", analysis_id)
        error = "The analysis failed because of an internal error. Please try again."
    await set_status(analysis_id, status="failed", error=error)


async def set_status(analysis_id: uuid.UUID, **values) -> None:
    async with Session() as db:
        await db.execute(update(Analysis).where(Analysis.id == analysis_id).values(**values))
        await db.commit()


def parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def to_money(value: float | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def mask_account_number(number: str | None) -> str | None:
    digits = re.sub(r"\D", "", number or "")
    return f"******{digits[-4:]}" if len(digits) >= 4 else None


async def save_result(analysis_id: uuid.UUID, final_state: dict) -> None:
    ai_reading = final_state["ai_reading"]
    async with Session() as db:
        db.add_all(
            Transaction(analysis_id=analysis_id, position=i, date=parse_date(row["date"]),
                        narration=row["narration"], debit=to_money(row["debit"]), credit=to_money(row["credit"]),
                        balance=to_money(row["balance"]), category=row["category"])
            for i, row in enumerate(ai_reading["transactions"])
        )
        analysis_row = await db.get(Analysis, analysis_id)
        analysis_row.status = "completed"
        analysis_row.bank = ai_reading["bank_name"]
        analysis_row.account_name = ai_reading["account_name"]
        analysis_row.account_number_masked = mask_account_number(ai_reading["account_number"])
        analysis_row.period_start = parse_date(ai_reading["period_start"])
        analysis_row.period_end = parse_date(ai_reading["period_end"])
        analysis_row.risk_grade = final_state["grade"]
        analysis_row.report = final_state["report"]
        analysis_row.result = {
            "metrics": final_state["metrics"],
            "flags": final_state["flags"],
            "tamper": final_state["tamper"],
            "loan_check": final_state.get("loan_check"),
            "fix_attempts": final_state.get("fix_attempts", 0),
            "pdf_producer": final_state["producer"],
        }
        await db.commit()


@app.get("/analyses", response_model=list[AnalysisSummary])
async def list_analyses() -> list[Analysis]:
    async with Session() as db:
        return list(await db.scalars(select(Analysis).order_by(Analysis.created_at.desc()).limit(50)))


@app.get("/analyses/{analysis_id}", response_model=AnalysisDetail)
async def read_analysis(analysis_id: uuid.UUID) -> Analysis:
    async with Session() as db:
        analysis_row = await db.get(Analysis, analysis_id)
    if analysis_row is None:
        raise HTTPException(404, "Analysis not found.")
    return analysis_row


@app.get("/analyses/{analysis_id}/transactions", response_model=list[TransactionOut])
async def read_transactions(analysis_id: uuid.UUID) -> list[Transaction]:
    async with Session() as db:
        return list(await db.scalars(
            select(Transaction).where(Transaction.analysis_id == analysis_id).order_by(Transaction.position)
        ))
