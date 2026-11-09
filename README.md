# LoanLens

A GenAI app, built as an agentic workflow in LangGraph, that reads a Nigerian bank statement PDF and tells a lender
whether the applicant can afford a loan. It extracts every transaction, checks and corrects its own reading,
categorises spending, flags signs of tampering, checks the requested loan against what the applicant can afford,
and writes a credit report.

Loan officers do this by hand today: hours per applicant, in a different format for every bank, while some
applicants edit their PDFs to look richer.

## The workflow

A LangGraph graph in `backend/app/agent/graph.py`:

```
START -> read_statement -> check_balances ──(do not add up)──> fix_reading ─┐
                                 ^                                          │
                                 └──────────────────────────────────────────┘
                                 │ (all add up, or out of attempts)
                                 v
                              decide -> write_report -> END
```

| Step | Done by | What happens |
|---|---|---|
| read_statement | LLM | Reads the whole statement into a form: account details, summary box, and every transaction with a category |
| check_balances | Code | Checks every row: previous balance + money in - money out = printed balance |
| fix_reading | LLM, self-correction loop | Looks again at the rows that do not add up and fixes its own mistakes. If it says its reading was right, the statement itself does not add up, which is a tamper sign |
| decide | Code | Income, spending, debt-to-income, betting share, affordable repayment, tamper checks, loan check, risk grade |
| write_report | LLM | A credit report written from the computed facts only |

**The LLM reads and writes. Code does every calculation and every decision.**

The LLM earns its place in the reading step: one model handles any bank's layout and messy narrations such as
`NIP TRF FROM OLATUNJI DAMILOLA/ACCESS/REFUND`, where per-bank parsers and keyword rules break. Everything that
must be exact or auditable is plain Python.

## Loan check

The loan officer can enter the requested loan (amount, term, annual interest rate) with the statement. Code works
out the monthly instalment with the standard amortisation formula and compares it with the affordable repayment
(loan repayments may take at most a third of income):

| Requested | Instalment | Affordable | Result |
|---|---|---|---|
| ₦1,000,000, 12 months, 30% | ₦97,487 | ₦202,620 | Within capacity |
| ₦1,000,000, 3 months, 30% | ₦350,137 | ₦202,620 | Exceeds capacity: maximum on these terms ₦578,687 |

A loan that exceeds capacity is a risk flag. The officer makes the final decision.

## Keeping the model honest

- **Structured output**: every model reply must match a Pydantic schema.
- **Code checks the model's reading**: every balance is verified, and rows that do not add up are re-read.
- **Code decides**: all calculations, lending rules, the loan check and the grade are plain Python, never the model.
- **Grounded report** (`guardrails.py`): every figure in the report must appear in the facts code computed.
  If the model invents or rounds a number, the facts are shown instead.
- **Deterministic and bounded**: temperature 0, a 2-minute timeout per model call, at most 2 fix attempts.

Uploads are checked (PDF only, page limit, scanned images refused), and only the last 4 digits of the account
number are stored.

## Tamper checks

- The running balance breaks, and the model confirms it read the page correctly
- The summary box does not match the transactions
- The PDF was produced by an editing tool (iLovePDF, Word, Print to PDF, and others)

Any of these gives the grade **Reject**.

## Code map

Read the files in this order:

| # | File | What it is |
|---|---|---|
| 1 | `backend/app/config.py` | The database address and OpenAI key, read from `.env` |
| 2 | `backend/app/db.py` | Database connection |
| 3 | `backend/app/models.py` | The two tables: analyses and transactions |
| 4 | `backend/app/guardrails.py` | The report guardrail: no figure the model invents reaches the screen |
| 5 | `backend/app/rules.py` | Balance check, lending rules, loan check, tamper checks (plain code, no AI) |
| 6 | `backend/app/agent/prompts.py` | Instructions for the three AI steps |
| 7 | `backend/app/agent/graph.py` | The LangGraph workflow: the forms, the AI calls, the steps, the graph |
| 8 | `backend/app/api_models.py` | The shape of the API's replies |
| 9 | `backend/app/main.py` | FastAPI: upload, run the workflow, save, read results |
| 10 | `backend/ui/app.py` | Streamlit screen for loan officers |

`backend/samples/` has 12 fictional statements to try, including 4 tampered ones.

## Run it

Setup:

```bash
python -m venv .venv
.venv/Scripts/activate            # macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt
cd backend
cp .env.example .env              # add your OpenAI key and database URL
```

Database: Postgres, created once with Docker (matches the URL in `.env.example`, with `USER:PASSWORD` set to
`postgres:postgres`):

```bash
docker run -d --name loanlens-postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=statement_analyzer \
  -p 5433:5432 postgres:16
```

Without Docker, SQLite works for a quick try: `pip install aiosqlite` and set
`DATABASE_URL=sqlite+aiosqlite:///./demo.db` in `.env`.

Run the API and the UI in two terminals, both from `backend/`:

```bash
uvicorn app.main:app --port 8010      # API docs at http://127.0.0.1:8010/docs
streamlit run ui/app.py               # http://localhost:8501
```

Upload any PDF from `backend/samples/`, optionally with a requested loan. One analysis takes 1 to 4 minutes.

## Tracing (optional)

Add these to `backend/.env` to see every step, prompt, reply, token count and cost in LangSmith
(free tier), with no code changes:

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
```

## Limits and next steps

- The AI reads the whole statement in one call, and must return every transaction in one reply. That suits
  statements of up to about 150 transactions (a typical 3-month salary account), so uploads are capped at
  15 pages. The trader and 6-month samples may be too long. Next step: read page by page in parallel.
- Scanned or photographed statements are refused.
- The graph follows fixed steps with one self-correction loop; the model does not choose tools or plan. That is
  deliberate for a lending decision, which must be predictable and auditable.
- Next steps: an evaluation script that scores grades and tamper detection over the samples, and fast template
  parsers for the largest banks with the LLM as the fallback.
