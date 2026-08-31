"""Multi-channel dispatch pipeline for recovery communications.

Supports:
1. Email via Resend API (with automatic local sandbox fallback in runs/outbox/).
2. WhatsApp via WhatsApp Cloud API interactive button format (simulated sandbox).
3. Audit tracking on every dispatch attempt.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import audit, contacts
from app.config import PROJECT_ROOT
from app.envelope import Channel
from app.messages import DraftedMessage

logger = logging.getLogger(__name__)

OUTBOX_DIR = PROJECT_ROOT / "runs" / "outbox"


@dataclass(frozen=True)
class DispatchResult:
    success: bool
    channel: Channel
    message_id: str
    simulated: bool
    error: str | None = None
    payload: dict[str, Any] | None = None


def _ensure_outbox() -> Path:
    """The sandbox directory, falling back to the system temp dir on a read-only tree.

    Serverless bundles the application read-only and gives one writable path, so a
    hard-coded `runs/outbox` under the project root raises OSError there. The sandbox is a
    development convenience, not a record - the audit log is the record - so degrading to
    a temporary directory is right where failing the dispatch would not be.
    """
    try:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        return OUTBOX_DIR
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "recovery-outbox"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def dispatch_email(
    message: DraftedMessage,
    recipient_email: str,
    *,
    dry_run: bool = False,
) -> DispatchResult:
    """Dispatch email via Resend when armed, else write to the local outbox sandbox.

    The allowlist is applied here rather than at contact resolution, because this is the
    last point before the message leaves the process and it must hold whatever produced the
    address. When a redirect happens the intended recipient is preserved in the subject and
    a banner on the body, so a delivered message never hides who it was really aimed at.
    """
    msg_id = f"msg_{message.debtor_id}_{int(time.time() * 1000)}"
    from_email = os.getenv("FROM_EMAIL", "recovery@msme-agent.in")
    live = contacts.send_mode() == contacts.SendMode.LIVE and not dry_run

    delivery_email, redirected_from = contacts.resolve_delivery(recipient_email)
    subject = message.subject
    body = message.body
    if redirected_from:
        subject = f"[to {redirected_from}] {subject}"
        body = (
            f"[Delivery allowlist active: this message was addressed to {redirected_from} "
            f"and redirected to {delivery_email}. The copy below is unmodified.]\n\n{body}"
        )

    outbox = _ensure_outbox()
    file_path = outbox / f"{msg_id}.txt"
    file_path.write_text(
        f"To: {delivery_email}\nFrom: {from_email}\nSubject: {subject}\n\n{body}",
        encoding="utf-8",
    )

    last_error: str | None = None
    if live:
        try:
            import urllib.request

            req_payload = {
                "from": from_email,
                "to": [delivery_email],
                "subject": subject,
                "text": body,
            }
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=json.dumps(req_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {os.getenv('RESEND_API_KEY', '').strip()}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                remote_id = resp_data.get("id", msg_id)
                audit.record(
                    "channel.email_sent",
                    debtor_id=message.debtor_id,
                    recipient=delivery_email,
                    intended_recipient=redirected_from or delivery_email,
                    redirected=bool(redirected_from),
                    message_id=remote_id,
                    is_statutory=message.is_statutory,
                    simulated=False,
                )
                return DispatchResult(
                    success=True,
                    channel=Channel.EMAIL,
                    message_id=remote_id,
                    simulated=False,
                    payload=req_payload,
                )
        except Exception as exc:
            last_error = str(exc)
            logger.warning("Resend live dispatch failed (%s)", exc)
            audit.record(
                "channel.email_failed",
                debtor_id=message.debtor_id,
                recipient=delivery_email,
                intended_recipient=redirected_from or delivery_email,
                error=last_error,
            )
            return DispatchResult(
                success=False,
                channel=Channel.EMAIL,
                message_id=msg_id,
                simulated=False,
                error=last_error,
                payload=req_payload,
            )

    audit.record(
        "channel.email_sent",
        debtor_id=message.debtor_id,
        recipient=delivery_email,
        intended_recipient=redirected_from or delivery_email,
        redirected=bool(redirected_from),
        message_id=msg_id,
        is_statutory=message.is_statutory,
        simulated=True,
        sandbox_path=str(file_path),
    )
    return DispatchResult(
        success=True,
        channel=Channel.EMAIL,
        message_id=msg_id,
        simulated=True,
        error=None,
        payload={"to": delivery_email, "subject": subject, "body": body},
    )


def dispatch_whatsapp(
    message: DraftedMessage,
    recipient_phone: str,
    *,
    dry_run: bool = False,
) -> DispatchResult:
    """Format and dispatch WhatsApp interactive button payload."""
    msg_id = f"wa_{message.debtor_id}_{int(time.time() * 1000)}"
    outbox = _ensure_outbox()

    wa_payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "interactive",
        "dry_run": dry_run,
        "interactive": {
            "type": "button",
            "body": {"text": message.body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_resolve", "title": "Resolve Online"}},
                    {"type": "reply", "reply": {"id": "btn_promise", "title": "Promise Date"}},
                ]
            },
        },
    }

    file_path = outbox / f"{msg_id}.json"
    file_path.write_text(json.dumps(wa_payload, indent=2), encoding="utf-8")

    audit.record(
        "channel.whatsapp_sent",
        debtor_id=message.debtor_id,
        recipient=recipient_phone,
        message_id=msg_id,
        language=message.language.value,
        simulated=True,
        dry_run=dry_run,
        sandbox_path=str(file_path),
    )

    return DispatchResult(
        success=True,
        channel=Channel.WHATSAPP,
        message_id=msg_id,
        simulated=True,
        payload=wa_payload,
    )


def dispatch_message(
    message: DraftedMessage,
    recipient_email: str | None = None,
    recipient_phone: str | None = None,
    *,
    dry_run: bool = False,
) -> DispatchResult:
    """Route a drafted message to its target channel dispatcher."""
    if message.channel == Channel.EMAIL:
        target_email = recipient_email or getattr(message, "recipient_email", None)
        if not target_email:
            audit.record("channel.missing_recipient", debtor_id=message.debtor_id, channel="email")
            return DispatchResult(
                success=False,
                channel=Channel.EMAIL,
                message_id="",
                simulated=False,
                error="Missing recipient email address",
            )
        return dispatch_email(message, target_email, dry_run=dry_run)

    if message.channel == Channel.WHATSAPP:
        target_phone = recipient_phone or getattr(message, "recipient_phone", None)
        if not target_phone:
            audit.record("channel.missing_recipient", debtor_id=message.debtor_id, channel="whatsapp")
            return DispatchResult(
                success=False,
                channel=Channel.WHATSAPP,
                message_id="",
                simulated=False,
                error="Missing recipient phone number",
            )
        return dispatch_whatsapp(message, target_phone, dry_run=dry_run)

    if message.channel == Channel.PORTAL:
        msg_id = f"portal_{message.debtor_id}_{int(time.time() * 1000)}"
        audit.record(
            "channel.portal_notification_created",
            debtor_id=message.debtor_id,
            message_id=msg_id,
            subject=message.subject,
            is_statutory=message.is_statutory,
        )
        return DispatchResult(
            success=True,
            channel=Channel.PORTAL,
            message_id=msg_id,
            simulated=True,
            payload={"subject": message.subject, "body": message.body},
        )

    audit.record(
        "channel.suppressed_no_contact",
        debtor_id=message.debtor_id,
        channel=message.channel.value,
    )
    return DispatchResult(
        success=True,
        channel=Channel.NONE,
        message_id="",
        simulated=True,
        error=None,
    )
