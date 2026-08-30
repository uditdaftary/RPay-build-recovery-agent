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

import json
import logging
import os
import threading
from datetime import date, timedelta

import razorpay
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app import audit, config, razorpay_gateway
from app.config import PROJECT_ROOT, business_today
from app.contact_history import MAX_PROMISE_WINDOWS_WITHOUT_SETTLEMENT
from app.disputes import (
    DisputeCategory,
    classify_dispute_reason,
    recompute_statutory_dates_on_dispute,
)
from app.ledger import InvoiceState
from app.ledger import balance_paise as _balance_paise
from app.operator import (
    approve_review_item,
    export_audit_events,
    get_review_queue,
    is_kill_switch_active,
    reject_review_item,
    set_kill_switch,
)

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
            "contractual_due_date": inv.get("contractual_due_date"),
            "delivery_date": inv.get("delivery_date"),
            "written_agreement": inv.get("written_agreement", True),
            "status": "DISPUTED" if inv["state"] == str(InvoiceState.DISPUTED) else "OVERDUE",
        }
    return view


# Every ledger invoice is resolvable by /r/{invoice_id}, so any decision's link works. The
# index page shows only a curated few, named in DEMO_TOKENS, to keep the demo landing tight.
INVOICES: dict[str, dict] = _load_invoices()


def _demo_tokens() -> list[str]:
    """The handful of invoices the index page showcases.

    `DEMO_TOKENS` in the environment names them explicitly once the ledger has been eyeballed;
    absent that, the clearest chase cases stand in — the most overdue invoices with a live
    balance. A knob, not a hardcode, because which cases tell the story is a human's call.
    """
    override = os.getenv("DEMO_TOKENS", "").strip()
    if override:
        return [t.strip() for t in override.split(",") if t.strip() in INVOICES]
    chaseable = [
        t for t, inv in INVOICES.items()
        if inv["status"] == "OVERDUE" and _balance_paise(inv) > 0
    ]
    chaseable.sort(key=lambda t: INVOICES[t]["days_overdue"], reverse=True)
    return chaseable[:3]


DEMO_TOKENS: list[str] = _demo_tokens()


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


def _find_invoice(order_id: str) -> tuple[str, dict] | tuple[None, None]:
    for token, invoice in INVOICES.items():
        if order_id in invoice.get("razorpay_order_ids", ()):
            return token, invoice
    return None, None


# Guards the check-then-append below. Starlette runs sync routes in a threadpool even in
# one process, so two redeliveries of the same webhook can arrive genuinely concurrently;
# without this, both could read "not yet processed" before either records it, double
# crediting one payment.
_SETTLEMENT_LOCK = threading.Lock()


def suppress_on_settlement(token: str, invoice: dict, payment_id: str, amount_paise: int) -> None:
    """Apply a capture to an invoice: halt the ladder if it clears, else chase the remainder.

    Idempotent on `payment_id`, not on the PAID status alone: Razorpay redelivers webhooks,
    and a replayed *partial* leaves the status PARTIALLY_PAID, so a status check would let it
    through and double-count. A fully settled invoice ignores any further capture.

    A partial capture credits what arrived and records the balance still owed, so the
    strategist's live projection resumes on the remainder rather than the face value. A
    capture that clears the balance halts the ladder — the Razorpay differentiator: the money
    lands on the rail the agent controls, so chasing stops here, not on the next sweep.
    """
    with _SETTLEMENT_LOCK:
        processed = invoice.setdefault("processed_payment_ids", [])
        if payment_id in processed or invoice["status"] == "PAID":
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
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    rows = [
        {"token": t, "amount_display": format_inr(_balance_paise(INVOICES[t])), **INVOICES[t]}
        for t in DEMO_TOKENS
    ]
    return templates.TemplateResponse(request, "index.html", {"invoices": rows})


@app.get("/r/{token}", response_class=HTMLResponse)
def resolution_page(request: Request, token: str) -> HTMLResponse:
    invoice = INVOICES.get(token)
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
    invoice = INVOICES.get(body.token)
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
    invoice = INVOICES.get(body.token)
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
    invoice = INVOICES.get(body.token)
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
    elif invoice.get("contractual_due_date"):
        delivery_d = date.fromisoformat(invoice["contractual_due_date"]) - timedelta(days=45)
    else:
        delivery_d = business_today() - timedelta(days=invoice.get("days_overdue", 30))

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
# Operator Console: Review-First Mode, Kill Switch & Audit Exporter
# ---------------------------------------------------------------------------


class KillSwitchRequest(BaseModel):
    active: bool


class ReviewActionRequest(BaseModel):
    debtor_id: str
    reason: str | None = None


@app.get("/operator", response_class=HTMLResponse)
def operator_dashboard(request: Request) -> HTMLResponse:
    """Render operator dashboard with live kill switch and review queue."""
    return templates.TemplateResponse(
        request,
        "operator.html",
        {
            "kill_switch_active": is_kill_switch_active(),
            "queue": get_review_queue(),
        },
    )


@app.post("/api/operator/kill-switch")
def toggle_kill_switch(body: KillSwitchRequest) -> JSONResponse:
    """Engage or disengage the master agent kill switch immediately."""
    new_state = set_kill_switch(body.active)
    return JSONResponse({"kill_switch_active": new_state})


@app.get("/api/operator/queue")
def list_review_queue() -> JSONResponse:
    """Return all pending review-first actions."""
    return JSONResponse({"queue": get_review_queue()})


@app.post("/api/operator/approve")
def approve_action(body: ReviewActionRequest) -> JSONResponse:
    """Approve a review-first action and dispatch to communication channel."""
    res = approve_review_item(body.debtor_id)
    if res is None:
        return JSONResponse({"error": "debtor not found in review queue"}, status_code=404)
    return JSONResponse(res)


@app.post("/api/operator/reject")
def reject_action(body: ReviewActionRequest) -> JSONResponse:
    """Reject a review-first action with a logged reason."""
    reason = body.reason or "Operator manual rejection"
    ok = reject_review_item(body.debtor_id, reason)
    if not ok:
        return JSONResponse({"error": "debtor not found in review queue"}, status_code=404)
    return JSONResponse({"rejected": True, "debtor_id": body.debtor_id, "reason": reason})


@app.get("/api/operator/export")
def export_audit_log(format: str = "json") -> Response:
    """Export complete audit trail in JSON or CSV format."""
    exported = export_audit_events(format_type=format)
    media_type = "application/json" if format.lower() == "json" else "text/csv"
    return Response(content=exported, media_type=media_type)


