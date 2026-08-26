# B2B Receivables Recovery Agent

Razorpay AI Buildathon 2026, Track 03 (AI Revenue Recovery).

An agent for Indian B2B receivables that decides, per debtor rather than per invoice,
whether to wait, collect, negotiate, reconcile or escalate. It executes that decision on
Razorpay and halts itself the moment settlement lands, inside a hard policy envelope,
with a full audit trail, measured against a fixed baseline policy on an identical seed.

## What runs today

| Piece | Status |
|---|---|
| Razorpay order creation (test mode) | Working, verified against the live test API |
| Checkout modal on the debtor page | Working, opens against a real order |
| Checkout callback signature verification | Working, with checks |
| Webhook signature verification | Working, with checks |
| Suppression on settlement, idempotent | Working |
| Debtor resolution page: pay, promise, dispute | Working |
| Append-only audit log | Working |
| Seeded ledger, 70 invoices under 20 debtors | Working, reproducible from a seed |
| Model failover across a model chain | Working |
| Policy envelope, strategist, baseline runner | Working, checked offline. All eight guardrails enforced |

One gap worth naming: the card has not been pushed through Razorpay's checkout iframe
end to end. Everything either side of it is verified, including signature verification
against a real order id, but the iframe is cross origin and needs a human.

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
```

| Variable | Needed for | Where it comes from |
|---|---|---|
| `RAZORPAY_KEY_ID` | Everything | Razorpay dashboard, test mode |
| `RAZORPAY_KEY_SECRET` | Orders, checkout callback | Razorpay dashboard, test mode |
| `RAZORPAY_WEBHOOK_SECRET` | Webhooks only | Set by you when creating the webhook. **Not the key secret.** |
| `GOOGLE_API_KEY` | The decision engine | https://aistudio.google.com/apikey |
| `LLM_MODEL` | The decision engine | Defaults to `gemini-3.6-flash` |
| `LLM_FALLBACK_MODELS` | Surviving a model outage | Defaults to `gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-latest`. The two `-lite` entries are unmeasured capacity headroom |
| `LLM_TIMEOUT_MS` | Bounding a slow call | Defaults to `20000` |

Note that `gemini-2.5-flash` appears in `models.list()` but returns 404 on
`generateContent`, so it is not a usable fallback.

## Run

```bash
python -m uvicorn app.server:app --reload --port 8000
```

Then open http://localhost:8000. `/health` reports which credentials are configured.

Generate the ledger:

```bash
python -m app.ledger --seed 42 --write
```

## Test

```bash
python test_signatures.py
```

Covers both signature algorithms against known-answer vectors, tampered payloads, the
wrong-secret case, the re-serialised-body bug, an unset webhook secret, amount
validation, and Indian digit grouping.

```bash
python -m app.ledger --check
```

Checks that the same seed reproduces the same ledger across processes, that different
seeds diverge, that all four failure states are present, that TDS invoices reconcile to
face value, that the statutory window is measured from acceptance, that exactly one
merchant can invoke the statute, and that the hidden behaviour parameters never reach the
agent's view.

```bash
python test_decisions.py
```

Checks that the hard policy envelope enforces deterministic guardrails (opt-out permanent
suppression, active dispute protection, MSMED trader refusal, TDS reconciliation, VIP
relationship protection that fails closed on unknown account value, no money ask on an
account already settled off-rail, a seven-day contact cooldown, an escalation ceiling, no
chasing inside a promise that has not fallen due, and every independent exclusion ground
surviving into the audit trail), that hidden behaviour parameters cannot reach a decision
and a debtor row
without an opt-out flag is refused outright, that a prohibited strategy and an ask outside
the pre-authorised band are both intercepted **and** left flagged for human review — the
band is bounded below by the concession floor and above by the collectible balance, so the
agent can neither invent a discount nor demand withheld TDS back, and a strategy that asks
for no money cannot carry an amount at all. Delivery is typed too: an unknown channel or a
deadline that is prose rather than a date fails validation, and a deadline already past is
dropped. Both decision paths write one `decision.made` shape, so the results page can read
every row alike. The baseline policy is checked to progress on calendar days overdue.

Runs offline with no model calls, so it costs nothing and cannot flake on a rate limit. The
live batch against the real model chain is opt-in:

```bash
RUN_LIVE_LLM_CHECKS=1 python test_decisions.py
```

It prints a per-case agent-versus-baseline table rather than asserting a divergence count.
Divergence measures difference, not correctness: the baseline escalates all 20 debtors, so
an agent that always answered `WAIT` would score 100%. The number that belongs in the pitch
is the per-case adjudication, including the cases where the agent loses.

### Razorpay test instruments

Test mode only. These never move real money.

| Instrument | Value |
|---|---|
| Card | `4100 2800 0000 1007`, CVV `123`, expiry `12/26` |
| UPI | `test@razorpay` |

## Manual steps still required

1. **Webhook secret and a public URL.** Razorpay cannot reach `localhost`, so the
   settlement path cannot be exercised against real Razorpay traffic until the service is
   reachable from the internet. The plan is to deploy behind a domain from the GitHub
   Student Developer Pack rather than a tunnel. Once it is up, add
   `https://<domain>/api/razorpay/webhook` under Settings then Webhooks in the dashboard,
   subscribe to `payment.captured`, set a secret, and copy that secret into `.env` as
   `RAZORPAY_WEBHOOK_SECRET`.

   The suppression path itself is already verified end to end against locally signed
   deliveries, including a forged signature rejected with 400 and a redelivery ignored as
   a duplicate. What the public URL adds is Razorpay as the sender.

2. **End-to-end card payment.** Needs a human to complete the checkout iframe. Test
   instruments are above.

3. **Rotate the Razorpay test keys before this repository is made public.** Test-mode
   keys still authenticate against a real account.

## Design notes

**The webhook is the system of record, not the checkout callback.**
`/api/verify-payment` confirms to the browser that the payment succeeded.
`/api/razorpay/webhook` is what tells the system, and it is the only one that arrives
when the payer closes the tab mid-redirect. Suppression therefore hangs off the webhook
alone. Redeliveries are idempotent, so a retry never double counts a recovery.

**Two secrets, two purposes.** `KEY_SECRET` signs the checkout callback over
`order_id|payment_id`. `WEBHOOK_SECRET` signs the webhook over the raw request body.
Confusing them fails in a way that looks like payments randomly breaking, so
`verify_webhook_signature` raises rather than returning `False` when the secret is unset.

**Signature comparison is constant time** via `hmac.compare_digest`, and the webhook
verifies the exact bytes received. Verifying a re-serialised body is the classic bug
here, and there is a test that fails if someone reintroduces it.

**The decision engine is provider-agnostic.** Razorpay's own Agent Studio is built on the
Claude Agent SDK, so parity with their stack stays reachable, but this build runs on
Google AI Studio. Swapping providers touches `app/llm.py` and nothing else, which is why
no other module imports `google.genai`.

**Model availability is measured, not assumed.** Structured-output calls on 2026-08-26:

| Model | Success | Median latency |
|---|---|---|
| `gemini-3.7-flash` | 0 of 8 | every call 504, unavailable rather than slow |
| `gemini-3.6-flash` | 3 of 3 | 7.4s |
| `gemini-3.5-flash` | 3 of 3 | 6.5s |

`gemini-3.7-flash` also 504'd at 122s against a 120s ceiling, so it is not merely slow.
Primary is `gemini-3.6-flash`. Every model that answered returned the same verdict on the
same prompt, so the fallback chain changes
whether a decision arrives, not what it is. Calls fail over across the chain on a bounded
clock and `llm.failover` is written to the audit log naming the model that answered. A
client-side read timeout is not an `APIError`, so it is caught explicitly; without that it
would bypass the chain entirely, which is the exact failure the chain exists to survive.

**The ledger separates what the agent sees from what is true.** Each debtor carries hidden
behaviour parameters fixed by the seed. `Debtor.agent_view()` is the only projection the
strategist may read, and a check fails if those parameters ever leak into it. Without that
separation, any recovery number is one the author chose rather than one the agent earned.

**No subresource integrity on `checkout.js`.** Razorpay ships that URL unversioned and
updates it in place, so a pinned hash would silently start failing payments on their next
deploy.

## Layout

```
app/
  config.py            environment, fails loudly on missing Razorpay credentials
  ledger.py            seeded synthetic ledger, hidden behaviour params, statutory dates
  envelope.py          hard policy envelope, deterministic guardrails, action classes
  contact_history.py   cooldown, intensity and open promises, derived from the audit log
  strategist.py        AI recovery strategist, per-debtor structured decision engine
  baseline.py          calendar-based baseline policy runner
  razorpay_gateway.py  order creation, both signature verifications
  llm.py               single model call site, model failover
  audit.py             append-only JSONL event log
  server.py            HTTP routes and the suppression hook
  templates/           debtor resolution page
data/ledger.json       generated ledger, committed so results are reproducible
test_signatures.py     runnable checks for the money path
test_decisions.py      runnable checks for the envelope, baseline, and decision divergence
```