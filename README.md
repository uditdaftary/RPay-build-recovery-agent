# B2B Receivables Recovery Agent

[![CI / Test Suite](https://img.shields.io/badge/tests-124%20passed-brightgreen.svg)](file:///test_decisions.py)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Razorpay API](https://img.shields.io/badge/Razorpay-REST%20%26%20Webhooks-0C2340.svg)](https://razorpay.com/)
[![Compliance](https://img.shields.io/badge/Compliance-MSMED%20s.15%2F16%20%2B%2043B(h)-navy.svg)](file:///app/statute.py)

An autonomous, legally grounded, and relationship-aware B2B receivables recovery agent built on Razorpay APIs, India statutory frameworks (MSMED Act 2006 & Section 43B(h)), and Gemini AI reasoning.

---

## 1. Problem Statement

Indian Micro and Small Enterprises (MSMEs) face severe working-capital stress, with an estimated ₹8.1 trillion in delayed receivables across supply chains. Traditional collections workflows fail because they rely on blunt, calendar-based dunning sequences that:
1. **Damage strategic buyer relationships** by sending aggressive legal threats for routine short-term delays.
2. **Double-bill compliant buyers** who legitimately withheld Tax Deducted at Source (TDS) or paid off-rail via NEFT/RTGS.
3. **Harm suppliers legally** by unlawfully asserting MSMED statutory penalties on ineligible categories (such as trading enterprises).
4. **Continue chasing after settlement** due to fragmented payment systems without real-time suppression.

The **B2B Receivables Recovery Agent** solves this with a two-stage decision architecture: a hard deterministic policy envelope that guarantees zero-harassment and statutory compliance, paired with an AI strategist that adapts tone, timing, and channels per debtor account.

---

## 2. Key Features

- **Two-Stage Safety Architecture:**
  - *Stage 1: Deterministic Policy Envelope (`app/envelope.py`)* — 11 hard rules enforcing opt-out suppression, active dispute halts, MSMED trader refusal, Form 26AS TDS reconciliation, 7-day contact cooldowns, and financial concession bounding.
  - *Stage 2: AI Recovery Strategist (`app/strategist.py`)* — Contextual reasoning evaluating debtor payment history, promise reliability, relationship tenure, and invoice aging.
- **Instant Webhook Suppression-on-Settlement (`app/server.py`):**
  - Consumes Razorpay `payment.captured` webhooks with raw-byte HMAC-SHA256 signature verification.
  - Thread-safe idempotent capture processing halts the recovery ladder in real time.
- **India MSMED Statutory Engine (`app/statute.py`, `app/disputes.py`):**
  - Calculates Section 15 statutory due dates (15-day deemed acceptance / 45-day written contract cap).
  - Enforces Section 16 compound monthly penal interest (3× RBI bank rate = 20.25% p.a.).
  - Couples with Section 43B(h) of the Income Tax Act for constructive, non-adversarial reminders.
  - Dynamically resets statutory clocks when valid written objections are lodged within 15 days of delivery.
- **Multi-Door Debtor Resolution Surface (`/r/{invoice_id}`):**
  - **Door 1 (Pay):** Direct Razorpay checkout (UPI, Card, NetBanking).
  - **Door 2 (Promise):** Structured Promise-to-Pay (PTP) commitment date selection (suppresses chasing until due).
  - **Door 3 (Dispute):** Structured objection submission with evidentiary requirements (halts ladder, routes to human triage).
- **Human Operator Surface & Master Kill Switch (`app/operator.py`, `/operator`):**
  - Real-time Master Kill Switch to instantly halt all automated outbound communications.
  - Review-First Mode queue for high-stakes decisions (statutory notices, significant concessions).
  - Tamper-evident append-only JSONL audit trail with JSON/CSV export.
- **Evaluation & Counterfactual Benchmark (`run_experiment.py`, `/results`):**
  - Deterministic evaluation harness comparing AI Agent vs Calendar Baseline policy across portfolio metrics, strategy distribution, and 8 debtor archetypes.

---

## 3. Quickstart & Installation

### Prerequisites
- Python 3.11+
- Git

### Installation

```bash
# Clone the repository
git clone https://github.com/your-username/recovery-agent.git
cd recovery-agent

# Install dependencies
pip install -r requirements.txt
```

### Environment Configuration

Create a `.env` file from `.env.example`:

```bash
cp .env.example .env
```

Configure the following variables:

| Variable | Description | Source / Default |
|---|---|---|
| `RAZORPAY_KEY_ID` | Razorpay API Key ID | Razorpay Dashboard (Test Mode) |
| `RAZORPAY_KEY_SECRET` | Razorpay API Key Secret | Razorpay Dashboard (Test Mode) |
| `RAZORPAY_WEBHOOK_SECRET` | Secret for verifying incoming webhooks | Configured in Razorpay Webhook settings |
| `GOOGLE_API_KEY` | Google Gemini API Key | https://aistudio.google.com/apikey |
| `LLM_MODEL` | Primary AI Strategist model | `gemini-3.6-flash` |
| `LLM_FALLBACK_MODELS` | Failover model hierarchy | `gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite` |
| `OPERATOR_API_KEY` | Operator console access key | `operator-secret-key` |

---

## 4. Running the Application

### 1. Generate the Synthetic Receivables Ledger

```bash
python -m app.ledger --seed 42 --write
```

Generates a reproducible 70-invoice, 20-debtor ledger with seeded failure states (TDS withholding, off-rail UTR payments, disputes, partial settlements) with verified SHA-256 fingerprint `7c1314468565fc3d`.

### 2. Start the FastAPI Server

```bash
python -m uvicorn app.server:app --reload --port 8000
```

Access the application surfaces:
- **Landing & Demo Portals:** http://localhost:8000/
- **Debtor Multi-Door Resolution:** http://localhost:8000/r/INV-4008
- **Human Operator Console:** http://localhost:8000/operator?key=operator-secret-key
- **Counterfactual Benchmark Dashboard:** http://localhost:8000/results
- **Health Check:** http://localhost:8000/health

---

## 5. Running the Evaluation Benchmark

Run the automated evaluation benchmark comparing the AI Recovery Strategist against the Calendar Baseline Policy:

```bash
# Formatted Table Output (Default)
python run_experiment.py --seed 42

# Markdown Report Output
python run_experiment.py --seed 42 --output markdown

# Structured JSON Output
python run_experiment.py --seed 42 --output json --save runs/benchmark_report.json
```

### Evaluation Highlights (Seed 42)
- **Restraint Share:** 10.0% conscious `WAIT` restraint vs 0.0% baseline noise.
- **Reconciliation Routing:** 40.0% of accounts routed to `RECONCILE` for Form 26AS TDS & UTR verification.
- **Prevented Escalations:** 20/20 unnecessary statutory escalations avoided.
- **Touch Efficiency:** 0.03 touches per ₹1 Lakh collected (vs 0.04 baseline).

---

## 6. Test Instruments (Razorpay Test Mode)

Use these credentials in Razorpay Test Mode checkout:

| Instrument | Details |
|---|---|
| **Test Card** | Number: `4100 2800 0000 1007`, Expiry: `12/26`, CVV: `123` |
| **Test UPI** | VPA: `test@razorpay` (or any valid test handle) |
| **NetBanking** | Select any test bank (e.g., HDFC, ICICI, SBI) |

---

## 7. Verification & Testing

Execute the comprehensive 5-gate pre-submission verification suite:

```bash
python verify_all.py
```

Runs all 5 validation gates:
1. **Unit Test Suite:** 124+ automated tests covering signature cryptography, statutory calculations, envelope guardrails, and message generation.
2. **Ruff Lint & Formatting:** Strict codebase linting and datetime timezone safety.
3. **Ledger Determinism:** SHA-256 fingerprint verification against seed 42.
4. **Benchmark Reproducibility:** End-to-end execution of `run_experiment.py`.
5. **Hygiene & Leak Scanner:** Zero private planning document leaks or hardcoded secrets.

To run individual test suites:

```bash
# Run all unit tests
python -m pytest

# Run cryptography & signature verification tests
python test_signatures.py

# Run statutory & MSMED calculation tests
python -m pytest test_statute.py

# Run policy envelope & decision tests
python -m pytest test_decisions.py

# Run repository hygiene scanner
python -m pytest test_hygiene.py
```

---

## 8. Repository Layout

```
recovery-agent/
├── app/
│   ├── config.py            # Environment settings and business calendar
│   ├── ledger.py            # Synthetic ledger generator, empirical BehaviourParams, fingerprint
│   ├── envelope.py          # Stage 1: Deterministic policy envelope & 11 guardrails
│   ├── strategist.py        # Stage 2: AI Recovery Strategist per-debtor decision engine
│   ├── messages.py          # Stage 3: Outbound copy drafting & anti-dark-pattern filter
│   ├── channels.py          # Channel dispatchers (Email, WhatsApp, Portal stubs)
│   ├── statute.py           # MSMED s.15/16 penal interest & Section 43B(h) calculator
│   ├── disputes.py          # Dispute categorization & statutory clock recomputation
│   ├── contact_history.py   # Debtor contact intensity, cooldowns, and open PTP tracking
│   ├── baseline.py          # Calendar-based baseline dunning policy
│   ├── operator.py          # Operator console, review queue, kill switch, audit export
│   ├── mandate.py           # Mandate retry sequencer stub & failure categorization
│   ├── razorpay_gateway.py  # Razorpay REST client & signature verification
│   ├── llm.py               # Google GenAI client with multi-model failover chain
│   ├── audit.py             # Append-only JSONL event logging
│   ├── server.py            # FastAPI application, webhooks, debtor resolution & results routes
│   └── templates/           # Jinja2 templates (index, resolution, operator, results)
├── data/
│   └── ledger.json          # Seeded synthetic receivables ledger
├── ARCHITECTURE.md          # Exhaustive technical systems architecture documentation
├── run_experiment.py        # Benchmark harness & comparative evaluation runner
├── verify_all.py            # 5-gate pre-submission verification script
├── test_hygiene.py          # Secret & private document leak scanner
├── test_signatures.py       # Signature cryptography tests
├── test_decisions.py        # Decision engine & envelope tests
├── test_statute.py          # MSMED & statutory calculation tests
├── test_channels.py         # Communication channel tests
├── test_operator.py         # Operator console & kill switch tests
├── test_results_ui.py       # Benchmark API & results UI tests
├── pyproject.toml           # Project metadata & ruff configuration
└── requirements.txt         # Production dependencies
```

---

## 9. License

This project is licensed under the Apache 2.0 License.