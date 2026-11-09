CATEGORIES = (
    "salary", "business_income", "other_income", "loan_received",
    "loan_repayment", "betting", "spending", "bank_charges",
)

MAX_DEBT_TO_INCOME = 0.33
MAX_BETTING_PCT = 10
MIN_MONTHS = 3

EDITING_TOOLS = ("ilovepdf", "smallpdf", "sejda", "microsoft word", "print to pdf", "canva", "adobe acrobat")


def find_breaks(ai_reading: dict) -> list[str]:
    """Previous balance + credit - debit must equal the printed balance on every row."""
    rows = ai_reading["transactions"]
    balance = ai_reading["opening_balance"]
    if balance is None:  # derive it from the first row
        first = rows[0]
        balance = first["balance"] - (first["credit"] or 0) + (first["debit"] or 0)

    breaks = []
    for row in rows:
        expected = balance + (row["credit"] or 0) - (row["debit"] or 0)
        if abs(expected - row["balance"]) > 0.01:  # allow for kobo rounding
            breaks.append(f"{row['date']} {row['narration']}: printed balance {row['balance']:,.2f}, "
                          f"expected {expected:,.2f}")
        balance = row["balance"]
    return breaks


def affordability(rows: list[dict]) -> dict:
    months = len({row["date"][:7] for row in rows})

    def monthly(category: str) -> float:
        total = sum((row["credit"] or row["debit"] or 0) for row in rows if row["category"] == category)
        return total / months

    income = monthly("salary") + monthly("business_income")
    repayments = monthly("loan_repayment")
    betting = monthly("betting")
    spending = sum(row["debit"] or 0 for row in rows) / months

    return {
        "months": months,
        "monthly_income": round(income, 2),
        "monthly_spending": round(spending, 2),
        "monthly_loan_repayments": round(repayments, 2),
        "debt_to_income_pct": round(repayments / income * 100, 1) if income else 0,
        "betting_pct": round(betting / income * 100, 1) if income else 0,
        "affordable_monthly_repayment": round(max(0, income * MAX_DEBT_TO_INCOME - repayments), 2),
    }


def monthly_instalment(amount: float, months: int, annual_rate_pct: float) -> float:
    """Standard amortised loan repayment."""
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return amount / months
    return amount * r / (1 - (1 + r) ** -months)


def loan_check(loan: dict, affordable: float) -> dict:
    """Can the applicant afford the loan they asked for? If not, what is the most they could borrow?"""
    instalment = monthly_instalment(loan["amount"], loan["months"], loan["rate"])
    max_amount = affordable / monthly_instalment(1, loan["months"], loan["rate"])
    return {
        **loan,
        "monthly_instalment": round(instalment, 2),
        "within_capacity": instalment <= affordable,
        "max_amount": round(max_amount, 2),
    }


def risk_flags(m: dict) -> list[str]:
    flags = []
    if m["months"] < MIN_MONTHS:
        flags.append(f"Only {m['months']} month(s) of history; at least {MIN_MONTHS} are needed.")
    if m["monthly_income"] == 0:
        flags.append("No salary or business income found.")
    if m["debt_to_income_pct"] > MAX_DEBT_TO_INCOME * 100:
        flags.append(f"Loan repayments take {m['debt_to_income_pct']}% of income (limit 33%).")
    if m["betting_pct"] > MAX_BETTING_PCT:
        flags.append(f"Betting equals {m['betting_pct']}% of income.")
    return flags


def tamper_signs(ai_reading: dict, balances_confirmed_wrong: bool, producer: str) -> list[str]:
    rows = ai_reading["transactions"]
    signs = []
    if balances_confirmed_wrong:
        signs.append("The balances do not add up, and the AI confirmed it read them correctly. "
                     "Genuine bank statements always add up.")

    credits = sum(row["credit"] or 0 for row in rows)
    if ai_reading["total_credits"] is not None and abs(ai_reading["total_credits"] - credits) > 0.01:
        signs.append(f"The summary box says money in was {ai_reading['total_credits']:,.2f}, "
                     f"but the transactions add up to {credits:,.2f}.")
    debits = sum(row["debit"] or 0 for row in rows)
    if ai_reading["total_debits"] is not None and abs(ai_reading["total_debits"] - debits) > 0.01:
        signs.append(f"The summary box says money out was {ai_reading['total_debits']:,.2f}, "
                     f"but the transactions add up to {debits:,.2f}.")

    if any(tool in producer.lower() for tool in EDITING_TOOLS):
        signs.append(f"The PDF was made with an editing tool ({producer}), not by a bank system.")
    return signs


def risk_grade(flags: list[str], tamper: list[str]) -> str:
    if tamper:
        return "Reject"
    if len(flags) >= 2:
        return "High risk"
    return "Medium risk" if flags else "Low risk"
