import json
from typing import Literal, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app import rules
from app.agent import prompts
from app.config import get_settings
from app.guardrails import invented_numbers

class TransactionForm(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    narration: str
    debit: float | None = Field(description="Money that left the account")
    credit: float | None = Field(description="Money that came into the account")
    balance: float = Field(description="Balance printed on this row")
    category: Literal[rules.CATEGORIES]  # type: ignore[valid-type]


class StatementForm(BaseModel):
    bank_name: str | None
    account_name: str | None
    account_number: str | None
    period_start: str | None = Field(description="YYYY-MM-DD")
    period_end: str | None = Field(description="YYYY-MM-DD")
    opening_balance: float | None
    total_credits: float | None = Field(description="Total money in, from the summary box")
    total_debits: float | None = Field(description="Total money out, from the summary box")
    transactions: list[TransactionForm]


class CorrectionForm(BaseModel):
    reading_was_correct: bool
    corrected_reading: StatementForm


class ModelUnavailable(Exception):
    pass


class DocumentRejected(Exception):
    pass


def chat_model():
    key = get_settings().openai_api_key
    if not key:
        raise ModelUnavailable("OPENAI_API_KEY is empty. Add it to backend/.env and restart the API.")
    return ChatOpenAI(model="gpt-4o-mini", api_key=key, temperature=0, timeout=120, max_retries=2)


class State(TypedDict, total=False):
    text: str
    producer: str
    loan: dict | None  # requested amount, months, rate; None if the officer left it blank
    ai_reading: dict
    breaks: list[str]
    fix_attempts: int
    confirmed: bool  # model re-checked and stands by its reading
    metrics: dict
    flags: list[str]
    tamper: list[str]
    loan_check: dict | None
    grade: str
    report: str


async def read_statement(state: State) -> dict:
    ai = chat_model().with_structured_output(StatementForm)
    filled_form = await ai.ainvoke([("system", prompts.READ), ("user", state["text"])])
    return {"ai_reading": filled_form.model_dump()}


async def check_balances(state: State) -> dict:
    if not state["ai_reading"]["transactions"]:
        raise DocumentRejected("No transactions were found in this statement.")
    return {"breaks": rules.find_breaks(state["ai_reading"])}


MAX_FIX_ATTEMPTS = 2


def after_check(state: State) -> str:
    if state["breaks"] and not state.get("confirmed") and state.get("fix_attempts", 0) < MAX_FIX_ATTEMPTS:
        return "fix_reading"
    return "decide"


async def fix_reading(state: State) -> dict:
    message = (f"{state['text']}\n\nYour reading:\n{json.dumps(state['ai_reading'])}\n\n"
               "Rows where the balance does not add up:\n" + "\n".join(state["breaks"]))
    ai = chat_model().with_structured_output(CorrectionForm)
    answer = await ai.ainvoke([("system", prompts.FIX), ("user", message)])
    attempts = state.get("fix_attempts", 0) + 1
    if answer.reading_was_correct:
        return {"confirmed": True, "fix_attempts": attempts}
    return {"ai_reading": answer.corrected_reading.model_dump(), "fix_attempts": attempts}


async def decide(state: State) -> dict:
    ai_reading = state["ai_reading"]
    numbers = rules.affordability(ai_reading["transactions"])
    warnings = rules.risk_flags(numbers)
    if state["breaks"] and not state.get("confirmed"):
        warnings.append("Some rows could not be read with certainty. Check them in the PDF.")
    check = None
    if state.get("loan"):
        check = rules.loan_check(state["loan"], numbers["affordable_monthly_repayment"])
        if not check["within_capacity"]:
            warnings.append(f"The requested loan's instalment of ₦{check['monthly_instalment']:,.2f} is more than "
                            f"the affordable ₦{numbers['affordable_monthly_repayment']:,.2f}.")
    fake_signs = rules.tamper_signs(ai_reading, state.get("confirmed", False), state["producer"])
    return {"metrics": numbers, "flags": warnings, "tamper": fake_signs, "loan_check": check,
            "grade": rules.risk_grade(warnings, fake_signs)}


async def write_report(state: State) -> dict:
    ai_reading, numbers, check = state["ai_reading"], state["metrics"], state.get("loan_check")
    loan_facts = [
        f"Requested loan: ₦{check['amount']:,.2f} over {check['months']} months at {check['rate']}% a year",
        f"Monthly instalment: ₦{check['monthly_instalment']:,.2f}",
        "Verdict: " + ("within capacity" if check["within_capacity"] else "exceeds capacity"),
        f"Maximum loan on these terms: ₦{check['max_amount']:,.2f}",
    ] if check else ["Requested loan: not given"]
    facts = "\n".join([
        f"Applicant: {ai_reading['account_name']}, {ai_reading['bank_name']}",
        f"Period: {ai_reading['period_start']} to {ai_reading['period_end']} ({numbers['months']} months)",
        f"Risk grade: {state['grade']}",
        f"Average monthly income: ₦{numbers['monthly_income']:,.2f}",
        f"Average monthly spending: ₦{numbers['monthly_spending']:,.2f}",
        f"Average monthly loan repayments: ₦{numbers['monthly_loan_repayments']:,.2f}",
        f"Debt-to-income: {numbers['debt_to_income_pct']}%",
        f"Betting: {numbers['betting_pct']}% of income",
        f"Affordable new monthly repayment: ₦{numbers['affordable_monthly_repayment']:,.2f}",
        "Risk flags: " + ("; ".join(state["flags"]) or "none"),
        "Tamper signs: " + ("; ".join(state["tamper"]) or "none"),
        *loan_facts,
    ])
    reply = await chat_model().ainvoke([("system", prompts.REPORT), ("user", facts)])
    report_text = reply.content
    if invented_numbers(report_text, facts):
        report_text = "The AI's report used numbers that were not in the facts, so here are the facts:\n\n" + facts
    return {"report": report_text}


def build_graph():
    graph = StateGraph(State)
    graph.add_node("read_statement", read_statement)
    graph.add_node("check_balances", check_balances)
    graph.add_node("fix_reading", fix_reading)
    graph.add_node("decide", decide)
    graph.add_node("write_report", write_report)

    graph.add_edge(START, "read_statement")
    graph.add_edge("read_statement", "check_balances")
    graph.add_conditional_edges("check_balances", after_check, ["fix_reading", "decide"])
    graph.add_edge("fix_reading", "check_balances")  # self-correction loop
    graph.add_edge("decide", "write_report")
    graph.add_edge("write_report", END)
    return graph.compile()


pipeline = build_graph()
