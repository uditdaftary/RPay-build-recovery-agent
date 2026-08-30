"""Operator state manager, Review-First queue, and Master Kill Switch.

Implements Razorpay Agent Design Principles:
1. "Turn off any agent at any time. One tap. Immediate." (Master Kill Switch).
2. "Review-first mode for high-stakes actions." (Review Queue).
3. Exportable audit logs in JSON and CSV.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from dataclasses import asdict
from typing import Any

from app import audit
from app.channels import dispatch_message
from app.messages import DraftedMessage

_OPERATOR_LOCK = threading.Lock()
_KILL_SWITCH_ACTIVE: bool = False
_REVIEW_QUEUE: dict[str, list[dict[str, Any]]] = {}


def set_kill_switch(active: bool) -> bool:
    """Set the master kill switch state immediately across all pipeline dispatchers."""
    global _KILL_SWITCH_ACTIVE
    with _OPERATOR_LOCK:
        _KILL_SWITCH_ACTIVE = active
        audit.record(
            "system.kill_switch_activated" if active else "system.kill_switch_deactivated",
            timestamp=int(time.time()),
        )
    return _KILL_SWITCH_ACTIVE


def is_kill_switch_active() -> bool:
    """Check whether the master kill switch is currently engaged."""
    with _OPERATOR_LOCK:
        return _KILL_SWITCH_ACTIVE


def queue_for_review(
    debtor_id: str,
    debtor_name: str,
    strategy: str,
    ask_amount_paise: int,
    reasoning: str,
    draft: DraftedMessage,
    recipient_email: str | None = None,
    recipient_phone: str | None = None,
    debtor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Place a high-stakes action into the operator review-first queue."""
    email = (
        recipient_email
        or getattr(draft, "recipient_email", None)
        or (debtor.get("email") if debtor else None)
    )
    phone = (
        recipient_phone
        or getattr(draft, "recipient_phone", None)
        or (debtor.get("phone") if debtor else None)
    )
    with _OPERATOR_LOCK:
        item = {
            "debtor_id": debtor_id,
            "debtor_name": debtor_name,
            "strategy": strategy,
            "ask_amount_paise": ask_amount_paise,
            "ask_amount_display": f"Rs {ask_amount_paise // 100:,}",
            "reasoning": reasoning,
            "channel": draft.channel.value,
            "language": draft.language.value,
            "tone": draft.tone.value,
            "is_statutory": draft.is_statutory,
            "subject": draft.subject,
            "body": draft.body,
            "recipient_email": email,
            "recipient_phone": phone,
            "queued_at": int(time.time()),
        }
        _REVIEW_QUEUE.setdefault(debtor_id, []).append(item)
        audit.record(
            "operator.review_queued",
            debtor_id=debtor_id,
            strategy=strategy,
            is_statutory=draft.is_statutory,
        )
    return item


def get_review_queue() -> list[dict[str, Any]]:
    """Retrieve all items currently waiting in review-first mode."""
    with _OPERATOR_LOCK:
        return [item for items in _REVIEW_QUEUE.values() for item in items]


def approve_review_item(debtor_id: str) -> dict[str, Any] | None:
    """Approve a queued action and dispatch outbound communication immediately."""
    with _OPERATOR_LOCK:
        items = _REVIEW_QUEUE.get(debtor_id)
        if not items:
            return None

        if _KILL_SWITCH_ACTIVE:
            audit.record(
                "operator.approval_blocked_by_kill_switch",
                debtor_id=debtor_id,
            )
            return {"approved": False, "error": "master kill switch is currently active"}

        item = items.pop(0)
        if not items:
            _REVIEW_QUEUE.pop(debtor_id, None)

    from app.envelope import Channel, Language, Tone

    draft = DraftedMessage(
        debtor_id=item["debtor_id"],
        channel=Channel(item["channel"]),
        language=Language(item["language"]),
        tone=Tone(item["tone"]),
        subject=item["subject"],
        body=item["body"],
        is_statutory=item["is_statutory"],
        dark_pattern_clean=True,
        recipient_email=item.get("recipient_email"),
        recipient_phone=item.get("recipient_phone"),
    )

    res = dispatch_message(
        draft,
        recipient_email=item.get("recipient_email"),
        recipient_phone=item.get("recipient_phone"),
    )
    audit.record(
        "operator.review_approved",
        debtor_id=debtor_id,
        strategy=item["strategy"],
        dispatched_message_id=res.message_id,
    )
    return {"approved": True, "dispatch_result": asdict(res)}


def reject_review_item(debtor_id: str, reason: str = "Operator manual rejection") -> bool:
    """Reject a queued action and log the human decision reason."""
    with _OPERATOR_LOCK:
        items = _REVIEW_QUEUE.get(debtor_id)
        if not items:
            return False

        item = items.pop(0)
        if not items:
            _REVIEW_QUEUE.pop(debtor_id, None)

    audit.record(
        "operator.review_rejected",
        debtor_id=debtor_id,
        strategy=item["strategy"],
        reason=reason,
        operator_reason=reason,
    )
    return True


def export_audit_events(format_type: str = "json") -> str:
    """Export complete append-only audit trail in JSON or CSV format."""
    events = audit.read_all()
    if format_type.lower() == "csv":
        output = io.StringIO()
        standard_keys = ["event", "ts", "debtor_id", "invoice_id", "strategy", "reason", "error"]
        other_keys: list[str] = []
        for e in events:
            for k in e:
                if k not in standard_keys and k not in other_keys:
                    other_keys.append(k)
        fieldnames = standard_keys + other_keys
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for e in events:
            writer.writerow(e)
        return output.getvalue()

    return json.dumps(events, indent=2, default=str)
