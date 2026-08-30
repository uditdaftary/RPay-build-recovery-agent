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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import audit
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
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    return OUTBOX_DIR


def dispatch_email(
    message: DraftedMessage,
    recipient_email: str,
    *,
    dry_run: bool = False,
) -> DispatchResult:
    """Dispatch email via Resend if configured, else write to local outbox sandbox."""
    resend_key = os.getenv("RESEND_API_KEY", "").strip()
    msg_id = f"msg_{message.debtor_id}_{int(time.time() * 1000)}"
    from_email = os.getenv("FROM_EMAIL", "recovery@msme-agent.in")

    outbox = _ensure_outbox()
    file_path = outbox / f"{message.debtor_id}_email_{int(time.time())}.txt"
    file_path.write_text(
        f"To: {recipient_email}\nFrom: {from_email}\nSubject: {message.subject}\n\n{message.body}",
        encoding="utf-8",
    )

    if resend_key and not dry_run:
        try:
            import urllib.request

            req_payload = {
                "from": from_email,
                "to": [recipient_email],
                "subject": message.subject,
                "text": message.body,
            }
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=json.dumps(req_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {resend_key}",
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
                    recipient=recipient_email,
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
            logger.warning("Resend live dispatch failed (%s); falling back to sandbox", exc)
            audit.record(
                "channel.email_fallback",
                debtor_id=message.debtor_id,
                error=str(exc),
            )

    audit.record(
        "channel.email_sent",
        debtor_id=message.debtor_id,
        recipient=recipient_email,
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
        payload={"to": recipient_email, "subject": message.subject, "body": message.body},
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

    file_path = outbox / f"{message.debtor_id}_whatsapp_{int(time.time())}.json"
    file_path.write_text(json.dumps(wa_payload, indent=2), encoding="utf-8")

    audit.record(
        "channel.whatsapp_sent",
        debtor_id=message.debtor_id,
        recipient=recipient_phone,
        message_id=msg_id,
        language=message.language.value,
        simulated=True,
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
        target_email = recipient_email or f"{message.debtor_id.lower()}@example.com"
        return dispatch_email(message, target_email, dry_run=dry_run)

    if message.channel == Channel.WHATSAPP:
        target_phone = recipient_phone or "+919876543210"
        return dispatch_whatsapp(message, target_phone, dry_run=dry_run)

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
