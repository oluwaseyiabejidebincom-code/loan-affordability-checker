import os
import time
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
API_URL = os.getenv("API_URL", "http://127.0.0.1:8010")
GRADE_ICON = {"Low risk": "🟢", "Medium risk": "🟠", "High risk": "🔴", "Reject": "⛔"}

st.set_page_config(page_title="LoanLens", page_icon="🏦", layout="wide")


def api(method: str, path: str, **kwargs):
    try:
        response = httpx.request(method, f"{API_URL}{path}", timeout=60, **kwargs)
    except httpx.HTTPError:
        st.error(f"Cannot reach the API at {API_URL}. Is it running?")
        st.stop()
    if response.status_code >= 400:
        st.error(response.json().get("detail", response.text))
        return None
    return response.json()


def naira(value) -> str:
    return f"₦{float(value):,.2f}"


with st.sidebar:
    st.header("New analysis")
    upload = st.file_uploader("Bank statement (PDF)", type=["pdf"])
    st.caption("Requested loan (optional)")
    loan_amount = st.number_input("Loan amount (₦)", min_value=0, step=50_000)
    loan_months = st.number_input("Term (months)", min_value=1, max_value=120, value=12)
    interest_rate = st.number_input("Interest rate (% per year)", min_value=0.0, max_value=200.0, value=30.0)
    if upload and st.button("Analyse", type="primary", width="stretch"):
        loan = {"loan_amount": loan_amount, "loan_months": loan_months, "interest_rate": interest_rate}
        created = api("POST", "/analyses", files={"file": (upload.name, upload.getvalue(), "application/pdf")},
                      data=loan if loan_amount else None)
        if created:
            st.session_state.selected = created["id"]

    st.header("Recent")
    for item in api("GET", "/analyses") or []:
        icon = GRADE_ICON.get(item["risk_grade"], "⏳" if item["status"] in ("queued", "processing") else "❌")
        if st.button(f"{icon} {item['account_name'] or item['filename']}", key=item["id"], width="stretch"):
            st.session_state.selected = item["id"]

st.title("LoanLens")
selected = st.session_state.get("selected")
if not selected:
    st.write("Upload a customer's bank statement PDF to get an affordability and fraud check.")
    st.stop()

analysis = api("GET", f"/analyses/{selected}")
if analysis is None:
    st.stop()

if analysis["status"] in ("queued", "processing"):
    st.info(f"Analysing {analysis['filename']}… this usually takes under a minute.")
    time.sleep(2)
    st.rerun()

if analysis["status"] == "failed":
    st.error(f"Analysis of {analysis['filename']} failed: {analysis['error']}")
    st.stop()

result, m = analysis["result"], analysis["result"]["metrics"]
st.subheader(analysis["account_name"] or "Unknown applicant")
st.caption(f"{analysis['bank'] or 'Unknown bank'} · account {analysis['account_number_masked'] or '-'} · "
           f"{analysis['period_start']} to {analysis['period_end']} · {analysis['filename']}")

if result["tamper"]:
    st.error("⛔ **This statement may have been altered.** Ask for a statement sent directly by the bank.\n\n"
             + "\n".join(f"- {t}" for t in result["tamper"]))

check = result.get("loan_check")
if check:
    terms = f"{naira(check['amount'])} over {check['months']} months at {check['rate']}% a year"
    if check["within_capacity"]:
        st.success(f"✅ **Within capacity.** {terms}: instalment {naira(check['monthly_instalment'])} a month, "
                   f"of an affordable {naira(m['affordable_monthly_repayment'])}.")
    else:
        st.error(f"❌ **Exceeds capacity.** {terms}: instalment {naira(check['monthly_instalment'])} a month, "
                 f"but only {naira(m['affordable_monthly_repayment'])} is affordable. "
                 f"Maximum on these terms: **{naira(check['max_amount'])}**.")

cols = st.columns(4)
cols[0].metric("Risk grade", f"{GRADE_ICON.get(analysis['risk_grade'], '')} {analysis['risk_grade']}")
cols[1].metric("Avg monthly income", naira(m["monthly_income"]))
cols[2].metric("Affordable repayment / month", naira(m["affordable_monthly_repayment"]))
cols[3].metric("Debt-to-income", f"{m['debt_to_income_pct']}%")
cols = st.columns(4)
cols[0].metric("Avg monthly spend", naira(m["monthly_spending"]))
cols[1].metric("Loan repayments / month", naira(m["monthly_loan_repayments"]))
cols[2].metric("Betting share of income", f"{m['betting_pct']}%")
cols[3].metric("Months covered", m["months"])

report_tab, flags_tab, txn_tab = st.tabs(["Report", "Risk flags", "Transactions"])

with report_tab:
    st.markdown(analysis["report"])

with flags_tab:
    for flag in result["flags"] or ["No risk flags."]:
        st.write(f"- {flag}")
    if result["fix_attempts"]:
        st.info(f"The agent re-checked its own reading {result['fix_attempts']} time(s) "
                "because some balances did not add up.")

with txn_tab:
    txns = pd.DataFrame(api("GET", f"/analyses/{selected}/transactions") or [])
    if not txns.empty:
        chosen = st.multiselect("Categories", sorted(txns["category"].unique()))
        st.dataframe(txns[txns["category"].isin(chosen)] if chosen else txns, width="stretch", hide_index=True)
