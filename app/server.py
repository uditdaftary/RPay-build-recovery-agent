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

from datetime import date

import razorpay
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import audit, config, razorpay_gateway
from app.config import PROJECT_ROOT

app = FastAPI(title="B2B Receivables Recovery Agent")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

# ponytail: two hardcoded invoices so the money path is runnable end to end today.
# The seeded ledger replaces this dict and nothing else in this file.
DEMO_INVOICES: dict[str, dict] = {
    "tok_demo1": {
        "invoice_id": "INV-4821",
        "debtor": "Acme Industries Pvt Ltd",
        "supplier": "Nandi Precision Components",
        "amount_paise": 380000_00,
        "days_overdue": 18,
        "status": "OVERDUE",
    },
    "tok_demo2": {
        "invoice_id": "INV-4903",
        "debtor": "Vertex Distributors",
        "supplier": "Nandi Precision Components",
        "amount_paise": 47500_00,
        "days_overdue": 4,
        "status": "OVERDUE",
    },
}


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
    for token, invoice in DEMO_INVOICES.items():
        if invoice.get("razorpay_order_id") == order_id:
            return token, invoice
    return None, None


def suppress_on_settlement(token: str, invoice: dict, payment_id: str, amount_paise: int) -> None:
    """Halt the ladder for a settled invoice.

    Idempotent: Razorpay retries webhooks, and a retry must not double-log a recovery or
    re-open a closed promise.
    """
    if invoice["status"] == "PAID":
        audit.record(
            "settlement.duplicate_ignored",
            invoice_id=invoice["invoice_id"],
            payment_id=payment_id,
        )
        return

    invoice["status"] = "PAID"
    invoice["paid_payment_id"] = payment_id
    invoice["paid_amount_paise"] = amount_paise

    audit.record(
        "settlement.confirmed",
        invoice_id=invoice["invoice_id"],
        debtor=invoice["debtor"],
        payment_id=payment_id,
        amount_paise=amount_paise,
        # The differentiator: settlement lands on the rail the agent controls, so the
        # ladder stops here rather than on the next scheduled sweep.
        suppressed=["queued_followups", "open_promise", "escalation_ladder"],
        token=token,
    )


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "razorpay_key_id": config.RAZORPAY_KEY_ID,
        "webhook_secret_configured": bool(config.RAZORPAY_WEBHOOK_SECRET),
        "model_key_configured": bool(config.GOOGLE_API_KEY),
        "model": config.LLM_MODEL,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    rows = [
        {"token": token, "amount_display": format_inr(inv["amount_paise"]), **inv}
        for token, inv in DEMO_INVOICES.items()
    ]
    return templates.TemplateResponse(request, "index.html", {"invoices": rows})


@app.get("/r/{token}", response_class=HTMLResponse)
def resolution_page(request: Request, token: str) -> HTMLResponse:
    invoice = DEMO_INVOICES.get(token)
    if invoice is None:
        return HTMLResponse("<h1>This link is not valid.</h1>", status_code=404)

    audit.record("resolution_page.viewed", invoice_id=invoice["invoice_id"], token=token)
    return templates.TemplateResponse(
        request,
        "resolution.html",
        {
            "token": token,
            "invoice": invoice,
            "amount_display": format_inr(invoice["amount_paise"]),
            "razorpay_key_id": config.RAZORPAY_KEY_ID,
        },
    )


class CreateOrderRequest(BaseModel):
    token: str
    amount_paise: int | None = Field(
        default=None, description="Defaults to the full outstanding amount."
    )


@app.post("/api/create-order")
def create_order(body: CreateOrderRequest) -> JSONResponse:
    invoice = DEMO_INVOICES.get(body.token)
    if invoice is None:
        return JSONResponse({"error": "unknown token"}, status_code=404)

    amount = body.amount_paise or invoice["amount_paise"]
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

    invoice["razorpay_order_id"] = order["id"]
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
    ok = razorpay_gateway.verify_payment_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
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
    promised_amount_paise: int | None = None


@app.post("/api/promise")
def record_promise(body: PromiseRequest) -> JSONResponse:
    invoice = DEMO_INVOICES.get(body.token)
    if invoice is None:
        return JSONResponse({"error": "unknown token"}, status_code=404)

    amount = body.promised_amount_paise or invoice["amount_paise"]
    invoice["status"] = "PROMISED"
    invoice["promised_date"] = body.promised_date.isoformat()
    invoice["promised_amount_paise"] = amount

    audit.record(
        "promise.made",
        invoice_id=invoice["invoice_id"],
        debtor=invoice["debtor"],
        promised_date=body.promised_date.isoformat(),
        promised_amount_paise=amount,
    )
    return JSONResponse({"ok": True, "promised_date": body.promised_date.isoformat()})


class DisputeRequest(BaseModel):
    token: str
    reason: str


@app.post("/api/dispute")
def raise_dispute(body: DisputeRequest) -> JSONResponse:
    invoice = DEMO_INVOICES.get(body.token)
    if invoice is None:
        return JSONResponse({"error": "unknown token"}, status_code=404)

    invoice["status"] = "DISPUTED"
    invoice["dispute_reason"] = body.reason

    # Classification and the statutory-clock recalculation land here once the strategist
    # exists. Halting and routing to a human is correct on its own in the meantime.
    audit.record(
        "dispute.raised",
        invoice_id=invoice["invoice_id"],
        debtor=invoice["debtor"],
        reason=body.reason,
        escalation="halted",
        routed_to="human_review",
    )
    return JSONResponse({"ok": True})
