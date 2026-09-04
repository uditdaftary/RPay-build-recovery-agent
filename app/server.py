"""HTTP surface for the recovery agent.

Three things live here:

  1. The debtor resolution page. Every outbound message links here rather than to a bare
     payment link, because the debtor needs three doors, not one: pay, promise a date, or
     raise a dispute. Structured outcomes beat parsing free text out of replies.
  2. The Razorpay checkout endpoints (create order, verify callback signature).
  3. The webhook, which is the system of record for settlement.

On (3): the /api/verify-payment response tells the *browser* the payment worked. The
webhook is what tells the *system*, and it is the only one that arrives when the debtor
closes the tab mid-redirect. Suppression therefore hangs off the webhook alone.
"""

import hmac
import json
import logging
import os
import threading
from collections import OrderedDict
from copy import deepcopy
from datetime import date, timedelta

import razorpay
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app import audit, config, contacts, razorpay_gateway, store
from app.config import PROJECT_ROOT, business_today
from app.contact_history import MAX_PROMISE_WINDOWS_WITHOUT_SETTLEMENT
from app.disputes import (
    DisputeCategory,
    classify_dispute_reason,
    recompute_statutory_dates_on_dispute,
)
from app.envelope import Strategy
from app.ledger import InvoiceState, generate
from app.ledger import balance_paise as _balance_paise
from app.mandate import (
    MandateFailureCode,
    plan_mandate_retries,
)
from app.messages import draft_message_for_decision
from app.operator import (
    approve_review_item,
    export_audit_events,
    get_review_queue,
    is_kill_switch_active,
    reject_review_item,
    set_kill_switch,
)
from run_experiment import isolated_audit_log, run_experiment

DISPUTE_EVIDENCE_MAP: dict[DisputeCategory, str] = {
    DisputeCategory.GOODS_SERVICES: "Inspection Report / Lorry Receipt (LR) Copy / Damage Photos",
    DisputeCategory.INVOICE_MISMATCH: "Purchase Order (PO) Rate Copy / Pricing Agreement Sheet",
    DisputeCategory.DUPLICATE: "Original Settled Invoice Reference / Bank Payment Voucher",
    DisputeCategory.TAX_GST: "GSTR-2B Mismatch Certificate / Form 26AS TDS Credit Entry",
    DisputeCategory.ALREADY_PAID: "Bank Statement Copy / NEFT-RTGS UTR Transaction Voucher",
    DisputeCategory.WRONG_RECIPIENT: "GSTIN Certificate / Company Entity Verification Document",
    DisputeCategory.CONTRACTUAL: "Service Level Agreement (SLA) Clause / Milestone Sign-off Certificate",
    DisputeCategory.UNKNOWN: "Detailed Written Explanation & Supporting Invoices",
}


logger = logging.getLogger(__name__)

app = FastAPI(title="B2B Receivables Recovery Agent")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

def _load_invoices() -> dict[str, dict]:
    """The live payment surface, built from the seeded ledger and keyed by invoice id.

    The token IS the invoice id, so a strategist decision's `/r/{invoice_id}` link resolves
    with no second table. This is an in-memory view: status, promises and settlements mutate
    these dicts, never `data/ledger.json`, because the ledger is the experiment's seed and has
    to stay byte-identical. Single-worker, single-process only, which the demo is.

    `state` is the ledger's seeded classification; `status` is the runtime lifecycle this
    service moves. A seeded DISPUTED invoice opens with its door already shut; everything else
    opens chaseable. TDS-underpaid and off-rail invoices carry a zero live balance, which the
    balance helper and the strategist already read as nothing to collect.
    """
    ledger_path = PROJECT_ROOT / "data" / "ledger.json"
    if not ledger_path.exists():
        logger.warning("data/ledger.json not found; the resolution surface has no invoices")
        return {}
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    debtors = {d["debtor_id"]: d for d in ledger["debtors"]}
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}
    view: dict[str, dict] = {}
    for inv in ledger["invoices"]:
        debtor = debtors[inv["debtor_id"]]
        merchant = merchants[inv["merchant_id"]]
        view[inv["invoice_id"]] = {
            "invoice_id": inv["invoice_id"],
            "debtor": debtor["name"],
            "debtor_id": inv["debtor_id"],
            "supplier": merchant["name"],
            "amount_paise": inv["amount_paise"],
            "amount_received_paise": inv.get("amount_received_paise", 0),
            "tds_deducted_paise": inv.get("tds_deducted_paise", 0),
            "days_overdue": inv["days_overdue"],
            "invoice_date": inv.get("invoice_date"),
            "contractual_due_date": inv.get("contractual_due_date"),
            "delivery_date": inv.get("delivery_date"),
            "written_agreement": inv.get("written_agreement", True),
            "status": "DISPUTED" if inv["state"] == str(InvoiceState.DISPUTED) else "OVERDUE",
        }
    return view


# Every ledger invoice is resolvable by /r/{invoice_id}, so any decision's link works. The
# index page shows only a curated few, named in DEMO_TOKENS, to keep the demo landing tight.
INVOICES: dict[str, dict] = _load_invoices()


def demo_tokens() -> list[str]:
    """The handful of invoices the index page and the global nav showcase.

    Resolved per request, not once at import. The durable store outlives the process, so
    an invoice settled in an earlier session is still OVERDUE in the ledger this module
    loaded: pinning the list at import pointed the nav's "Live Demo" link at a paid
    invoice with a zero balance and a "Pay ₹0.00" button.

    `DEMO_TOKENS` in the environment names them explicitly once the ledger has been
    eyeballed; absent that, the clearest chase cases stand in - the most overdue invoices
    with a live balance. A knob, not a hardcode, because which cases tell the story is a
    human's call.
    """
    _hydrate_invoices()
    override = os.getenv("DEMO_TOKENS", "").strip()
    if override:
        named = [t.strip() for t in override.split(",") if t.strip() in INVOICES]
        if named:
            return named
    chaseable = [
        t for t, inv in INVOICES.items()
        if inv["status"] == "OVERDUE" and _balance_paise(inv) > 0
    ]
    chaseable.sort(key=lambda t: INVOICES[t]["days_overdue"], reverse=True)
    return chaseable[:3]


def demo_token() -> str:
    """The one resolution page the global nav links to from every surface.

    A callable, because base.html renders on pages whose routes know nothing about the
    ledger, and because a token pinned at import 404s the moment the ledger is
    regenerated - which is exactly what a hardcoded `INV-101` did.
    """
    live = demo_tokens()
    return live[0] if live else next(iter(INVOICES), "")


templates.env.globals["demo_token"] = demo_token

# How many review-queue rows the console renders inline. Each row embeds a full drafted
# message body, so the page grows by kilobytes per item; the API serves the rest.
OPERATOR_QUEUE_PAGE_SIZE = 25


# The debtor-facing bounds. Kept together and above the page that publishes them into the
# form, so a reader of resolution_page can see what limits it is rendering.
#
# A promise is no longer only a record. `contact_history` folds `promise.made` into
# DebtorHistory and the envelope excludes every money ask while the promise has not fallen
# due, so this endpoint writes a control input for the decision engine. It is public and
# unauthenticated, which makes an unbounded date a way to switch recovery off: one request
# naming a date in 2099 suppressed the account permanently. Ninety days is longer than any
# commercial payment term this product targets.
MAX_PROMISE_HORIZON_DAYS = 90

# One crore. Above this the figure is a typo or an attack, not a commitment, and it is
# rendered into the envelope's own exclusion reasoning.
MAX_PROMISE_PAISE = 1_00_00_000_00

DISPUTE_REASON_MAX = 2000


@app.exception_handler(RequestValidationError)
def rejected_input(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Answer a rejected request in the shape the resolution page already reads.

    FastAPI's default is 422 with a `detail` list, but every hand-written failure here
    returns `{"error": ...}` and the page renders that. Bounding the promise date and the
    dispute length therefore started showing debtors a raw "Request failed with status
    422" - the one surface in this system a debtor actually sees. One handler rather than
    a try/except per endpoint, because the mismatch is in the contract, not the route.
    """
    errors = exc.errors()
    first = errors[0] if errors else {}
    message = str(first.get("msg", "That value was not accepted."))
    # Logged, not recorded. These endpoints are public and unauthenticated, and the audit
    # log is now an input to the envelope that `contact_history` re-parses in full on every
    # decision - so an audit row per rejected request is an anonymous way to grow the file
    # the decision engine reads. Same reasoning `read_all` and `contact_history` already
    # apply to malformed rows: a defect in the caller belongs where defects go, not in the
    # evaluation artifact. Every field is named, because repeated probing is the pattern
    # worth seeing.
    logger.warning(
        "rejected %s: fields=%s messages=%s",
        request.url.path,
        [".".join(str(part) for part in error.get("loc", ())) for error in errors],
        [str(error.get("msg", "")) for error in errors],
    )
    # Pydantic prefixes its own validators; the debtor does not need the machinery.
    return JSONResponse({"error": message.removeprefix("Value error, ")}, status_code=422)


def _mutation_blocked(invoice: dict, *, allow_disputed: bool = False) -> JSONResponse | None:
    """The state gate the three debtor-facing endpoints share, or None if the write may run.

    They all mutate one `status` field and each grew its own guard set, so a promise landing
    on a disputed invoice rewrote the status to PROMISED and reopened the payment path that
    `create_order` had just been taught to close. `raise_dispute` is the one caller allowed
    to touch a disputed invoice, because restating a dispute changes nothing.
    """
    if invoice["status"] == "PAID":
        return JSONResponse({"error": "this invoice is already settled"}, status_code=400)
    if invoice["status"] == "DISPUTED" and not allow_disputed:
        return JSONResponse({"error": "this invoice has an open dispute"}, status_code=400)
    return None


def format_inr(paise: int) -> str:
    """Format paise as rupees with Indian digit grouping (12,34,567.00)."""
    rupees, remainder = divmod(paise, 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts + [tail])
    return f"{digits}.{remainder:02d}"


def _lookup_invoice(token: str) -> dict | None:
    """An invoice with its durable lifecycle folded in. The only way to read INVOICES."""
    _hydrate_invoices()
    return INVOICES.get(token)


def _find_invoice(order_id: str) -> tuple[str, dict] | tuple[None, None]:
    _hydrate_invoices()
    for token, invoice in INVOICES.items():
        if order_id in invoice.get("razorpay_order_ids", ()):
            return token, invoice
    return None, None


# Guards the check-then-append below. Starlette runs sync routes in a threadpool even in
# one process, so two redeliveries of the same webhook can arrive genuinely concurrently;
# without this, both could read "not yet processed" before either records it, double
# crediting one payment.
_SETTLEMENT_LOCK = threading.Lock()


# The fields `INVOICES` mutates at runtime. Everything else on an invoice comes from
# data/ledger.json and never changes, because the ledger is the experiment's seed and has
# to stay byte-identical - so only these are written to the durable store.
RUNTIME_INVOICE_FIELDS = (
    "status",
    "amount_received_paise",
    "promised_date",
    "promised_amount_paise",
    "promise_windows",
    "processed_payment_ids",
    "razorpay_order_ids",
    "dispute_reason",
    "dispute_category",
    "evidence_required",
    "statutory_clock_suspended",
)


def _persist_invoice(invoice: dict) -> None:
    """Write one invoice's runtime lifecycle to the store, if there is one."""
    if not store.is_enabled():
        return
    state = {k: invoice[k] for k in RUNTIME_INVOICE_FIELDS if k in invoice}
    store.save_invoice_runtime(invoice["invoice_id"], state)


def _hydrate_invoices() -> None:
    """Fold the durable lifecycle back over the ledger-derived view.

    `INVOICES` is process memory, and on serverless the instance that serves the payment
    is rarely the one that served the page. Without this a settlement recorded by the
    webhook is invisible to the next request and the debtor keeps being chased.
    """
    if not store.is_enabled():
        return
    for invoice_id, state in store.load_invoice_runtime().items():
        invoice = INVOICES.get(invoice_id)
        if invoice is not None:
            invoice.update(state)


def suppress_on_settlement(token: str, invoice: dict, payment_id: str, amount_paise: int) -> None:
    """Apply a capture to an invoice: halt the ladder if it clears, else chase the remainder.

    Idempotent on `payment_id`, not on the PAID status alone: Razorpay redelivers webhooks,
    and a replayed *partial* leaves the status PARTIALLY_PAID, so a status check would let it
    through and double-count. A fully settled invoice ignores any further capture.

    A partial capture credits what arrived and records the balance still owed, so the
    strategist's live projection resumes on the remainder rather than the face value. A
    capture that clears the balance halts the ladder â€” the Razorpay differentiator: the money
    lands on the rail the agent controls, so chasing stops here, not on the next sweep.
    """
    with _SETTLEMENT_LOCK:
        processed = invoice.setdefault("processed_payment_ids", [])
        # The replay guard. In one process the list plus the lock is enough; across
        # serverless instances neither is shared, so the database decides instead and the
        # conditional insert is the lock. `claim_payment` returns False when this capture
        # has already been applied by any instance.
        already_seen = (
            not store.claim_payment(invoice["invoice_id"], payment_id)
            if store.is_enabled()
            else payment_id in processed
        )
        if already_seen or invoice["status"] == "PAID":
            audit.record(
                "settlement.duplicate_ignored",
                invoice_id=invoice["invoice_id"],
                payment_id=payment_id,
            )
            return
        processed.append(payment_id)

        invoice["amount_received_paise"] = invoice.get("amount_received_paise", 0) + amount_paise
        remaining = _balance_paise(invoice)

        if remaining > 0:
            invoice["status"] = str(InvoiceState.PARTIALLY_PAID)
            _persist_invoice(invoice)
            audit.record(
                "settlement.partial",
                invoice_id=invoice["invoice_id"],
                debtor=invoice["debtor"],
                debtor_id=invoice["debtor_id"],
                payment_id=payment_id,
                amount_paise=amount_paise,
                remaining_paise=remaining,
                token=token,
            )
            return

        invoice["status"] = "PAID"
        _persist_invoice(invoice)
        audit.record(
            "settlement.confirmed",
            invoice_id=invoice["invoice_id"],
            debtor=invoice["debtor"],
            debtor_id=invoice["debtor_id"],
            payment_id=payment_id,
            amount_paise=amount_paise,
            suppressed=["queued_followups", "open_promise", "escalation_ladder"],
            token=token,
        )


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "key_configured": bool(config.RAZORPAY_KEY_ID),
        "webhook_secret_configured": bool(config.RAZORPAY_WEBHOOK_SECRET),
        "model_key_configured": bool(config.GOOGLE_API_KEY),
        "model": config.LLM_MODEL,
        # Whether state survives this process. False on a serverless deployment with no
        # DATABASE_URL, where the audit log the envelope folds is scratch space and the
        # kill switch only ever halts the instance that served the request.
        "durable_state": store.is_enabled(),
        "audit_log_ephemeral": audit.ephemeral_fallback_active(),
        "send_mode": contacts.send_mode(),
        "delivery_allowlist_configured": bool(contacts.allowed_recipient()),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    rows = [
        {"token": t, "amount_display": format_inr(_balance_paise(INVOICES[t])), **INVOICES[t]}
        for t in demo_tokens()
    ]
    return templates.TemplateResponse(request, "index.html", {"invoices": rows})


@app.get("/r/{token}", response_class=HTMLResponse)
def resolution_page(request: Request, token: str) -> HTMLResponse:
    invoice = _lookup_invoice(token)
    if invoice is None:
        return HTMLResponse("<h1>This link is not valid.</h1>", status_code=404)

    audit.record("resolution_page.viewed", invoice_id=invoice["invoice_id"], token=token)
    return templates.TemplateResponse(
        request,
        "resolution.html",
        {
            "token": token,
            "invoice": invoice,
            # The balance still owed, not the face value: a part-paid invoice shows and
            # charges the remainder.
            "amount_display": format_inr(_balance_paise(invoice)),
            "razorpay_key_id": config.RAZORPAY_KEY_ID,
            # The form constrains what the validators already enforce, and the window is
            # computed here rather than in the browser: the validator compares against this
            # machine's clock, so a debtor in another timezone was offered a first or last
            # day the server then refused.
            "promise_min": business_today().isoformat(),
            "promise_max": (business_today() + timedelta(days=MAX_PROMISE_HORIZON_DAYS)).isoformat(),
            "dispute_reason_max": DISPUTE_REASON_MAX,
        },
    )


class CreateOrderRequest(BaseModel):
    token: str
    # gt=0 rather than an unbounded int: a request naming 0 is a request for nothing, and
    # the `or` idiom below would have read it as "no amount given" and charged the lot.
    amount_paise: int | None = Field(
        default=None, gt=0, description="Defaults to the full outstanding amount."
    )


@app.post("/api/create-order")
def create_order(body: CreateOrderRequest) -> JSONResponse:
    invoice = _lookup_invoice(body.token)
    if invoice is None:
        return JSONResponse({"error": "unknown token"}, status_code=404)

    blocked = _mutation_blocked(invoice)
    if blocked is not None:
        return blocked

    # `is None`, not truthiness. See the field definition: 0 is an answer, not a silence.
    # Defaults to and is capped at the balance still owed, not the face value, so a debtor
    # settling the remainder of a part-paid invoice is neither overcharged nor able to
    # overpay.
    balance = _balance_paise(invoice)
    # A TDS_UNDERPAID or PAID_OFF_RAIL invoice reconciles to a live balance of 0 but is not
    # blocked by _mutation_blocked (its status is "OVERDUE", not "PAID"), so without this a
    # zero-balance invoice reached the gateway with amount_paise=0 and surfaced Razorpay's
    # raw "amount_paise must be >= 100" ValueError to the debtor.
    if balance <= 0:
        return JSONResponse(
            {"error": "there is nothing left to collect on this invoice"}, status_code=400
        )
    amount = balance if body.amount_paise is None else body.amount_paise
    if amount > balance:
        return JSONResponse(
            {"error": "amount exceeds the invoice balance"}, status_code=400
        )
    try:
        order = razorpay_gateway.create_order(
            amount_paise=amount,
            receipt=invoice["invoice_id"],
            notes={"invoice_id": invoice["invoice_id"], "debtor": invoice["debtor"]},
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except razorpay.errors.BadRequestError as exc:
        audit.record("order.rejected", invoice_id=invoice["invoice_id"], error=str(exc))
        return JSONResponse({"error": f"razorpay rejected the order: {exc}"}, status_code=400)
    except Exception as exc:
        # Fail loud: an unlogged gateway error here looks like a silent UI hang.
        audit.record("order.failed", invoice_id=invoice["invoice_id"], error=repr(exc))
        return JSONResponse({"error": "could not reach the payment gateway"}, status_code=502)

    invoice.setdefault("razorpay_order_ids", []).append(order["id"])
    # Persisted so the webhook can match this order back to its invoice even when the
    # capture is delivered to a different instance than the one that created the order.
    _persist_invoice(invoice)
    audit.record(
        "order.created",
        invoice_id=invoice["invoice_id"],
        order_id=order["id"],
        amount_paise=amount,
    )
    return JSONResponse(
        {"order_id": order["id"], "amount": order["amount"], "currency": order["currency"]}
    )


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@app.post("/api/verify-payment")
def verify_payment(body: VerifyPaymentRequest) -> JSONResponse:
    """Confirm the checkout callback for the browser.

    Deliberately does NOT mark the invoice paid. The webhook does that. If this endpoint
    were the system of record, an abandoned redirect would leave a paid invoice being
    chased.
    """
    try:
        ok = razorpay_gateway.verify_payment_signature(
            body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
        )
    except RuntimeError as exc:
        # Mirrors the webhook's handling of the same class of failure. Without this the
        # debtor who has just paid gets an unhandled 500 on the one surface they see, and
        # the misconfiguration leaves no trace in the log at all.
        # The detail goes to the log, not to the caller. This endpoint is public and its
        # body is rendered straight onto the resolution page, so `str(exc)` would tell a
        # stranger which environment variable is missing and how the app is deployed.
        audit.record("checkout.misconfigured", error=str(exc))
        return JSONResponse(
            {"error": "payment confirmation is unavailable; the team has been notified"},
            status_code=500,
        )
    audit.record(
        "checkout.signature_verified" if ok else "checkout.signature_mismatch",
        order_id=body.razorpay_order_id,
        payment_id=body.razorpay_payment_id,
    )
    if not ok:
        return JSONResponse({"verified": False, "error": "signature mismatch"}, status_code=400)
    return JSONResponse({"verified": True})


@app.post("/api/razorpay/webhook")
async def razorpay_webhook(request: Request) -> JSONResponse:
    """System of record for settlement. Verified against the webhook secret, not the key secret."""
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        valid = razorpay_gateway.verify_webhook_signature(raw_body, signature)
    except RuntimeError as exc:
        audit.record("webhook.misconfigured", error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)

    if not valid:
        audit.record("webhook.signature_mismatch", body_bytes=len(raw_body))
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    payload = await request.json()
    event = payload.get("event", "")

    if event == "payment.captured":
        entity = payload["payload"]["payment"]["entity"]
        order_id = entity.get("order_id", "")
        token, invoice = _find_invoice(order_id)
        if invoice is None:
            audit.record("webhook.unmatched_order", event=event, order_id=order_id)
        else:
            suppress_on_settlement(token, invoice, entity["id"], entity["amount"])
    else:
        audit.record("webhook.ignored_event", event=event)

    # Always 200 on a verified delivery. A non-2xx makes Razorpay retry an event we
    # already accepted.
    return JSONResponse({"ok": True})




class PromiseRequest(BaseModel):
    token: str
    promised_date: date
    promised_amount_paise: int | None = Field(default=None, gt=0, le=MAX_PROMISE_PAISE)

    @field_validator("promised_date")
    @classmethod
    def _within_horizon(cls, value: date) -> date:
        # The wall clock, not the ledger's as_of: a debtor filling this in is committing in
        # real time, whatever fixed date the seeded experiment is pinned to. Read in the
        # business timezone so a host in UTC does not refuse today's date as already past.
        today = business_today()
        if value < today:
            raise ValueError("promised date is in the past")
        if value > today + timedelta(days=MAX_PROMISE_HORIZON_DAYS):
            raise ValueError(
                f"promised date is more than {MAX_PROMISE_HORIZON_DAYS} days away"
            )
        return value


@app.post("/api/promise")
def record_promise(body: PromiseRequest) -> JSONResponse:
    invoice = _lookup_invoice(body.token)
    if invoice is None:
        return JSONResponse({"error": "unknown token"}, status_code=404)
    blocked = _mutation_blocked(invoice)
    if blocked is not None:
        return blocked
    # Bounded against this invoice, not only against the crore ceiling on the field. The
    # figure is folded into DebtorHistory and rendered verbatim into the envelope's own
    # exclusion reasoning, so an unanchored number becomes part of the audit trail.
    if body.promised_amount_paise is not None and body.promised_amount_paise > _balance_paise(invoice):
        return JSONResponse(
            {"error": "promised amount exceeds the invoice balance"}, status_code=400
        )

    # The two rules the audit fold applies to `promise.made`, answered here so the debtor
    # is told rather than sent an "ok" for a commitment the decision engine has already
    # decided to ignore. This reads per-invoice memory, so it is the cheap front door;
    # `contact_history.build` stays the authority, because it survives a restart and folds
    # what was actually logged rather than what this process happens to remember.
    recorded = invoice.get("promised_date")
    open_until = date.fromisoformat(recorded) if recorded else None
    if open_until is not None and open_until >= business_today():
        # A running commitment may be brought forward, never pushed back. Pushing it back is
        # how one public endpoint held recovery off indefinitely: promise again each time the
        # date got close and the envelope never permits a money ask.
        if body.promised_date > open_until:
            return JSONResponse(
                {
                    "error": f"a payment date of {recorded} is already on record; "
                    "a new date has to be earlier than that one"
                },
                status_code=400,
            )
    else:
        # No commitment running, so this opens a fresh window. Bounded, because a debtor who
        # lets each date pass and immediately names another is not making commitments.
        # Counted across the debtor's invoices, because that is the scope the fold uses.
        # Per invoice, a debtor holding two would get twice the budget here and then be
        # quietly ignored by the fold, which is the humouring this check exists to stop.
        opened = sum(
            other.get("promise_windows", 0)
            for other in INVOICES.values()
            if other["debtor_id"] == invoice["debtor_id"]
        )
        if opened >= MAX_PROMISE_WINDOWS_WITHOUT_SETTLEMENT:
            return JSONResponse(
                {
                    "error": f"{opened} payment dates have already passed without payment; "
                    "this invoice needs to be settled or discussed with a person"
                },
                status_code=400,
            )
        invoice["promise_windows"] = opened + 1

    amount = (
        invoice["amount_paise"]
        if body.promised_amount_paise is None
        else body.promised_amount_paise
    )
    invoice["status"] = "PROMISED"
    invoice["promised_date"] = body.promised_date.isoformat()
    invoice["promised_amount_paise"] = amount
    _persist_invoice(invoice)

    audit.record(
        "promise.made",
        invoice_id=invoice["invoice_id"],
        debtor=invoice["debtor"],
        debtor_id=invoice["debtor_id"],
        promised_date=body.promised_date.isoformat(),
        promised_amount_paise=amount,
    )
    return JSONResponse({"ok": True, "promised_date": body.promised_date.isoformat()})


class DisputeRequest(BaseModel):
    token: str
    # Debtor-supplied free text, appended verbatim to the append-only audit log that the
    # envelope now reads on every decision, and bound for the strategist prompt once the
    # dispute handler reads `dispute_reason`. Bounded at both ends: an empty reason is not a
    # dispute, and an unbounded one is a way to grow the log a decision depends on.
    reason: str = Field(min_length=1, max_length=DISPUTE_REASON_MAX)


@app.post("/api/dispute")
def raise_dispute(body: DisputeRequest) -> JSONResponse:
    invoice = _lookup_invoice(body.token)
    if invoice is None:
        return JSONResponse({"error": "unknown token"}, status_code=404)
    blocked = _mutation_blocked(invoice, allow_disputed=True)
    if blocked is not None:
        return blocked

    category = classify_dispute_reason(body.reason)
    evidence = DISPUTE_EVIDENCE_MAP.get(category, DISPUTE_EVIDENCE_MAP[DisputeCategory.UNKNOWN])

    # Recompute statutory dates under MSMED Section 15
    delivery_raw = invoice.get("delivery_date")
    if delivery_raw:
        delivery_d = date.fromisoformat(delivery_raw)
    elif invoice.get("invoice_date"):
        delivery_d = date.fromisoformat(invoice["invoice_date"])
    elif invoice.get("contractual_due_date"):
        window = 45 if invoice.get("written_agreement", True) else 15
        delivery_d = date.fromisoformat(invoice["contractual_due_date"]) - timedelta(days=window)
    else:
        days_overdue = invoice.get("days_overdue") or 15
        delivery_d = business_today() - timedelta(days=days_overdue)

    objection_d = business_today()
    written = invoice.get("written_agreement", True)
    acc_d, due_d, app_d = recompute_statutory_dates_on_dispute(
        delivery_date=delivery_d,
        written_agreement=written,
        objection_date=objection_d,
    )
    statutory_clock_suspended = acc_d is None

    invoice["status"] = "DISPUTED"
    invoice["dispute_reason"] = body.reason
    invoice["dispute_category"] = category.value
    invoice["evidence_required"] = evidence
    invoice["statutory_clock_suspended"] = statutory_clock_suspended
    _persist_invoice(invoice)

    audit.record(
        "dispute.raised",
        invoice_id=invoice["invoice_id"],
        debtor=invoice["debtor"],
        debtor_id=invoice["debtor_id"],
        reason=body.reason,
        category=category.value,
        escalation="halted",
        routed_to="human_review",
    )
    audit.record(
        "dispute.statutory_clock_updated",
        invoice_id=invoice["invoice_id"],
        debtor_id=invoice["debtor_id"],
        acceptance_date=acc_d.isoformat() if acc_d else None,
        statutory_due_date=due_d.isoformat() if due_d else None,
        statutory_clock_suspended=statutory_clock_suspended,
    )

    return JSONResponse(
        {
            "ok": True,
            "category": category.value,
            "evidence_required": evidence,
            "statutory_clock_suspended": statutory_clock_suspended,
        }
    )


# ---------------------------------------------------------------------------
# Mandate Simulation Webhook
# ---------------------------------------------------------------------------


class MandateWebhookSimulateRequest(BaseModel):
    mandate_id: str
    failure_code: MandateFailureCode
    failure_date: date | None = None


@app.post("/api/mandate/simulate-webhook")
def simulate_mandate_webhook(body: MandateWebhookSimulateRequest) -> JSONResponse:
    f_date = body.failure_date or business_today()
    plan = plan_mandate_retries(body.mandate_id, body.failure_code, f_date)
    return JSONResponse(
        {
            "ok": True,
            "mandate_id": plan.mandate_id,
            "failure_code": plan.failure_code.value,
            "retry_dates": [d.isoformat() for d in plan.retry_dates],
            "strategy_notes": plan.strategy_notes,
        }
    )


# ---------------------------------------------------------------------------
# Operator Console: Review-First Mode, Kill Switch & Audit Exporter
# ---------------------------------------------------------------------------


def _extract_operator_key(request: Request) -> str | None:
    key = request.headers.get("X-Operator-Key")
    if key:
        return key.strip()
    auth = request.headers.get("Authorization")
    if auth:
        parts = auth.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    if "key" in request.query_params:
        return request.query_params["key"].strip()
    if "api_key" in request.query_params:
        return request.query_params["api_key"].strip()
    return None


def verify_operator_auth(request: Request) -> str:
    expected = os.getenv("OPERATOR_API_KEY", "").strip()
    key = _extract_operator_key(request)
    if not expected or not key or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized operator access")
    return key


class KillSwitchRequest(BaseModel):
    active: bool


class ReviewActionRequest(BaseModel):
    debtor_id: str
    reason: str | None = Field(default=None, max_length=2000)


@app.get("/operator", response_class=HTMLResponse)
def operator_dashboard(request: Request) -> HTMLResponse:
    """Render operator dashboard with live kill switch and review queue."""
    expected = os.getenv("OPERATOR_API_KEY", "").strip()
    key = _extract_operator_key(request)
    if not expected or not key or not hmac.compare_digest(key, expected):
        return HTMLResponse(
            "<h1>401 Unauthorized</h1><p>Invalid or missing operator credentials.</p>",
            status_code=401,
        )
    return templates.TemplateResponse(
        request,
        "operator.html",
        {
            "kill_switch_active": is_kill_switch_active(),
            "queue": get_review_queue(),
            "operator_key": key,
        },
    )


@app.post("/api/operator/kill-switch")
def toggle_kill_switch(
    body: KillSwitchRequest, _: str = Depends(verify_operator_auth)
) -> JSONResponse:
    """Engage or disengage the master agent kill switch immediately."""
    new_state = set_kill_switch(body.active)
    return JSONResponse({"kill_switch_active": new_state})


@app.get("/api/operator/queue")
def list_review_queue(_: str = Depends(verify_operator_auth)) -> JSONResponse:
    """Return all pending review-first actions."""
    return JSONResponse({"queue": get_review_queue()})


@app.post("/api/operator/approve")
def approve_action(
    body: ReviewActionRequest, _: str = Depends(verify_operator_auth)
) -> JSONResponse:
    """Approve a review-first action and dispatch to communication channel."""
    res = approve_review_item(body.debtor_id)
    if res is None:
        return JSONResponse({"error": "debtor not found in review queue"}, status_code=404)
    if res.get("approved") is False:
        return JSONResponse(res, status_code=409)
    return JSONResponse(res)


@app.post("/api/operator/reject")
def reject_action(
    body: ReviewActionRequest, _: str = Depends(verify_operator_auth)
) -> JSONResponse:
    """Reject a review-first action with a logged reason."""
    reason = body.reason or "Operator manual rejection"
    ok = reject_review_item(body.debtor_id, reason)
    if not ok:
        return JSONResponse({"error": "debtor not found in review queue"}, status_code=404)
    return JSONResponse({"rejected": True, "debtor_id": body.debtor_id, "reason": reason})


@app.get("/api/operator/export")
def export_audit_log(
    format: str = "json", _: str = Depends(verify_operator_auth)
) -> Response:
    """Export complete audit trail in JSON or CSV format."""
    exported = export_audit_events(format_type=format)
    media_type = "application/json" if format.lower() == "json" else "text/csv"
    headers = {"Content-Disposition": f'attachment; filename="audit_events.{format.lower()}"'}
    return Response(content=exported, media_type=media_type, headers=headers)


# ---------------------------------------------------------------------------
# Results & Benchmark Evaluation Endpoints
# ---------------------------------------------------------------------------

ARCHETYPE_DETAILS: dict[int, dict[str, str]] = {
    1: {
        "archetype": "TDS Deducted (Form 26AS Reconciliation)",
        "debtor_profile": "Deducted 10% TDS under Section 194C/J and remitted net balance. Regular commercial account.",
        "why_baseline_erred": "Blindly treated the statutory TDS withholding shortfall as a delinquent default and issued an aggressive escalation notice demanding unowed money.",
        "why_agent_won": "Recognized legitimate tax withholding, suppressed debt collection dunning, and requested Form 26AS certificate for seamless accounting reconciliation without friction.",
    },
    2: {
        "archetype": "Off-Rail NEFT Payment (UTR Verification)",
        "debtor_profile": "Paid full invoice balance via offline direct NEFT/RTGS bank transfer with UTR submitted.",
        "why_baseline_erred": "Missed direct bank credit because funds bypassed payment link; escalated and threatened legal action against an already settled customer.",
        "why_agent_won": "Flagged UTR reference on record, initiated banking ledger reconciliation, and avoided humiliating double-billing of a good payer.",
    },
    3: {
        "archetype": "Active Dispute (Dispute Triage & MSMED Clock Reset)",
        "debtor_profile": "Raised formal goods/service quality objection within 15-day statutory objection window under MSMED Section 15.",
        "why_baseline_erred": "Escalated demand notice with statutory compound penal interest despite open dispute, violating MSMED Section 15 deemed acceptance rules.",
        "why_agent_won": "Suspended statutory interest clock, paused collection chasing, and routed objection to human dispute triage with structured evidence checklist.",
    },
    4: {
        "archetype": "Trader Merchant Refusal (MSMED Exclusion)",
        "debtor_profile": "Supplier is a registered retail/wholesale trader (NIC 45-47), strictly ineligible for MSMED Section 15/16 benefits.",
        "why_baseline_erred": "Unlawfully threatened MSMED Section 15/16 3x RBI compound penal interest and Section 43B(h) tax disallowance on behalf of a non-manufacturing trader.",
        "why_agent_won": "Strictly enforced statutory eligibility bounds, refused unlawful legal threats, and calibrated communication to a firm commercial collection notice.",
    },
    5: {
        "archetype": "VIP Relationship Protection (<5% Exposure)",
        "debtor_profile": "Strategic enterprise buyer with â‚¹8.4 Cr annual turnover and <5% portfolio exposure. Minor invoice timing friction.",
        "why_baseline_erred": "Applied rigid calendar dunning ladder and sent abrasive escalation notice, jeopardizing multi-crore enterprise contract over minor timing friction.",
        "why_agent_won": "Policy envelope enforced VIP relationship protection, barring abrasive dunning and routing to polite collaborative reconciliation.",
    },
    6: {
        "archetype": "Opt-Out Debtor (Zero-Harassment Compliance)",
        "debtor_profile": "Registered explicit opt-out request under DPDP / TRAI commercial communication regulations.",
        "why_baseline_erred": "Repeatedly spammed debtor with automated WhatsApp and email demands, committing direct regulatory compliance violations.",
        "why_agent_won": "Enforced immediate, permanent contact suppression (WAIT with 0 touches), maintaining complete regulatory compliance.",
    },
    7: {
        "archetype": "Reliable Late Payer (Restraint over Noise)",
        "debtor_profile": "Habitual late payer (avg ~5 days late) with 7 of 7 promises kept; 100% historical settlement record.",
        "why_baseline_erred": "Sent premature intrusive dunning reminders on Day 30, generating unnecessary noise and annoying a loyal paying client.",
        "why_agent_won": "Exercised intelligent WAIT restraint, trusting debtor's verified payment cycle and achieving zero-touch recovery without friction.",
    },
    8: {
        "archetype": "Complex Mixed-State Account (Human Handoff)",
        "debtor_profile": "3+ open invoices across conflicting states (dispute + partial payment + overdue) with low promise reliability.",
        "why_baseline_erred": "Applied generic single-invoice escalation rule blind to composite account complexity, risking litigation.",
        "why_agent_won": "Recognized aggregate ambiguity exceeded autonomous confidence threshold and safely routed to human credit specialist (HUMAN_HANDOFF).",
    },
}


# The published demo run. Anything else on the HTTP surface is an operator-only request,
# because a benchmark run is a full portfolio evaluation and `seed` and `as_of` come
# straight off the query string.
DEFAULT_RESULTS_SEED = 42
DEFAULT_RESULTS_AS_OF = "2026-08-26"

# Bounded, and small on purpose. The key is (seed, as_of), both caller-supplied, so an
# unbounded dict here is a way to grow the heap one 13 KB benchmark at a time. FIFO
# eviction rather than true LRU: the working set is the default key plus whatever an
# operator is currently comparing against, and that fits either way.
_RESULTS_CACHE: OrderedDict[tuple[int, str], dict[str, object]] = OrderedDict()
_RESULTS_CACHE_MAX = 8


def _require_default_results_params(request: Request, seed: int, as_of: str, live_llm: bool) -> None:
    """Anything beyond the published demo run costs a full experiment, so it needs a key.

    `live_llm` spends real model budget; a non-default `seed` or `as_of` spends CPU and a
    cache slot. All three are unauthenticated query parameters, so they are gated together
    rather than one at a time.
    """
    if live_llm or seed != DEFAULT_RESULTS_SEED or as_of != DEFAULT_RESULTS_AS_OF:
        verify_operator_auth(request)


def get_results_data(
    seed: int = 42,
    as_of: str = "2026-08-26",
    live_llm: bool = False,
) -> dict[str, object]:
    """Execute evaluation run and return enriched benchmark results."""
    cache_key = (seed, as_of)
    if not live_llm and cache_key in _RESULTS_CACHE:
        # A copy, not the stored dict. Handing out the cached object makes every caller a
        # potential writer of every later caller's response.
        return deepcopy(_RESULTS_CACHE[cache_key])

    with isolated_audit_log():
        exp = run_experiment(seed=seed, as_of=as_of, live_llm=live_llm)

        ledger = generate(seed=seed)
        if as_of:
            ledger["as_of"] = as_of

        debtors_by_id = {d["debtor_id"]: d for d in ledger["debtors"]}
        merchants_by_id = {m["merchant_id"]: m for m in ledger["merchants"]}
        invoices_by_debtor: dict[str, list[dict]] = {}
        for inv in ledger["invoices"]:
            invoices_by_debtor.setdefault(inv["debtor_id"], []).append(inv)

        as_of_date = date.fromisoformat(exp.as_of) if exp.as_of else business_today()
        agent_by_id = {d.debtor_id: d for d in exp.agent_decisions}

        enriched_matrix: list[dict[str, object]] = []
        for item in exp.adjudication_matrix:
            c_id = item.get("case_id", 1)
            d_id = item["debtor_id"]
            debtor = debtors_by_id.get(d_id, {})
            merchant = merchants_by_id.get(
                debtor.get("merchant_id"),
                {"merchant_id": "UNKNOWN", "name": "Supplier", "udyam_registered": False},
            )
            invoices = invoices_by_debtor.get(d_id, [])

            agent_dec = agent_by_id.get(d_id)
            copy_preview = None
            if agent_dec and agent_dec.strategy not in (Strategy.WAIT, Strategy.HUMAN_HANDOFF):
                draft = draft_message_for_decision(
                    decision=agent_dec,
                    debtor=debtor,
                    invoices=invoices,
                    merchant=merchant,
                    as_of=as_of_date,
                )
                copy_preview = {
                    "subject": draft.subject,
                    "body": draft.body,
                    "channel": draft.channel.value if hasattr(draft.channel, "value") else str(draft.channel),
                    "language": draft.language.value if hasattr(draft.language, "value") else str(draft.language),
                    "tone": draft.tone.value if hasattr(draft.tone, "value") else str(draft.tone),
                    "is_statutory": draft.is_statutory,
                    "dark_pattern_clean": draft.dark_pattern_clean,
                }

            details = ARCHETYPE_DETAILS.get(c_id, {})
            enriched_matrix.append({
                **item,
                "archetype": details.get("archetype", item.get("case_name")),
                "debtor_profile": details.get("debtor_profile", ""),
                "why_baseline_erred": details.get("why_baseline_erred", ""),
                "why_agent_won": details.get("why_agent_won", ""),
                "drafted_copy_preview": copy_preview,
            })

        res_data = {
            "seed": exp.seed,
            "as_of": exp.as_of,
            "is_live_llm": exp.is_live_llm,
            "portfolio": {
                "total_book_value_paise": exp.portfolio.total_book_value_paise,
                "total_book_value_inr": exp.portfolio.book_value_inr,
                "total_naive_outstanding_paise": exp.portfolio.total_naive_outstanding_paise,
                "total_naive_outstanding_inr": exp.portfolio.naive_outstanding_inr,
                "total_collectible_paise": exp.portfolio.total_collectible_paise,
                "total_collectible_inr": exp.portfolio.collectible_inr,
                "total_debtors": exp.portfolio.total_debtors,
                "total_invoices": exp.portfolio.total_invoices,
            },
            "strategy_distribution": {
                "agent": exp.comparative_metrics.get("agent_distribution", {}),
                "baseline": exp.comparative_metrics.get("baseline_distribution", {}),
            },
            "comparative_metrics": exp.comparative_metrics,
            "adjudication_matrix": enriched_matrix,
        }
        if not live_llm:
            _RESULTS_CACHE[cache_key] = deepcopy(res_data)
            while len(_RESULTS_CACHE) > _RESULTS_CACHE_MAX:
                _RESULTS_CACHE.popitem(last=False)
        return res_data


@app.get("/api/results")
def get_api_results(
    request: Request,
    seed: int = 42,
    as_of: str = "2026-08-26",
    live_llm: bool = False,
) -> JSONResponse:
    """Return complete benchmark evaluation dataset in JSON format."""
    # Shape before authorisation: a malformed date is a 400 whoever asks, and answering it
    # with a 401 would tell a caller their date was fine when it was not.
    try:
        if as_of:
            date.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid as_of date parameter: {exc}") from exc
    _require_default_results_params(request, seed, as_of, live_llm)

    try:
        data = get_results_data(seed=seed, as_of=as_of, live_llm=live_llm)
        return JSONResponse(data)
    except Exception as exc:
        logger.exception("Failed to compute benchmark results: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/results", response_class=HTMLResponse)
def results_page(
    request: Request,
    seed: int = 42,
    as_of: str = "2026-08-26",
    live_llm: bool = False,
) -> HTMLResponse:
    """Render counterfactual benchmark evaluation dashboard."""
    # Shape before authorisation; see the note on the JSON endpoint.
    try:
        if as_of:
            date.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid as_of date parameter: {exc}") from exc
    _require_default_results_params(request, seed, as_of, live_llm)

    try:
        data = get_results_data(seed=seed, as_of=as_of, live_llm=live_llm)
    except Exception as exc:
        logger.exception("Failed to load results page data: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    operator_key = _extract_operator_key(request)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "data": data,
            "seed": seed,
            "as_of": as_of,
            "live_llm": live_llm,
            "portfolio": data["portfolio"],
            "comparative_metrics": data["comparative_metrics"],
            "strategy_distribution": data["strategy_distribution"],
            "adjudication_matrix": data["adjudication_matrix"],
            "operator_key": operator_key,
        },
    )





