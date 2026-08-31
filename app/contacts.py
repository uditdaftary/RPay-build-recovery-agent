"""Where a debtor's contact details come from, and the last gate before a message leaves.

Two separate concerns, deliberately kept apart, because either one alone can put a dunning
notice in a stranger's inbox:

  1. **Resolution.** Which address belongs to this debtor. Never the ledger:
     `data/ledger.json` is the experiment's seed and its seed-42 fingerprint is the
     reproducibility claim `verify_all.py` Gate 3 enforces, so contacts live beside it
     rather than inside it. A debtor with no entry is not guessed at - the pipeline routes
     them to a human, which is the behaviour the recipient-synthesis fix established.

  2. **Delivery allowlist.** Which addresses this deployment may actually write to.
     Resolution can be wrong - a stale contacts file, a typo, a debtor id collision - and
     the cost of being wrong is a legal threat sent to an uninvolved third party. So the
     allowlist is enforced at the dispatch boundary, independently of whatever resolution
     produced, and live sending refuses to arm without one.

`ALLOWED_RECIPIENT` is read from the environment and never committed: this repository goes
public at submission, and a personal address in the tree is a permanent disclosure.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

CONTACTS_PATH = PROJECT_ROOT / "data" / "contacts.json"


class SendMode:
    """How far an outbound message is allowed to travel."""

    SANDBOX = "sandbox"
    LIVE = "live"


def send_mode() -> str:
    """The configured mode, defaulting to the one that cannot reach anybody.

    Live sending additionally requires both a Resend key and an allowlist. Refusing to arm
    rather than silently degrading means a misconfigured deployment writes to the outbox
    and says so, instead of either mailing strangers or appearing to send nothing.
    """
    requested = os.getenv("SEND_MODE", SendMode.SANDBOX).strip().lower()
    if requested != SendMode.LIVE:
        return SendMode.SANDBOX
    if not os.getenv("RESEND_API_KEY", "").strip():
        logger.warning("SEND_MODE=live but RESEND_API_KEY is unset; staying in sandbox")
        return SendMode.SANDBOX
    if not allowed_recipient():
        logger.warning("SEND_MODE=live but ALLOWED_RECIPIENT is unset; staying in sandbox")
        return SendMode.SANDBOX
    return SendMode.LIVE


def allowed_recipient() -> str:
    """The single address this deployment may write to, or empty if none is configured."""
    return os.getenv("ALLOWED_RECIPIENT", "").strip()


def _load() -> dict[str, dict[str, Any]]:
    """Contacts from the environment first, then the gitignored file, else nothing.

    The environment wins because that is how a serverless deployment supplies them: there
    is no writable filesystem to put a file on.
    """
    raw = os.getenv("DEBTOR_CONTACTS", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Loud, because the consequence of silently reading no contacts is a pipeline
            # that routes the entire book to human review and looks merely cautious.
            logger.error("DEBTOR_CONTACTS is not valid JSON; no contacts resolved from it")
            return {}
    return _load_file(CONTACTS_PATH)


def _load_file(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.error("%s is not valid JSON; no contacts resolved from it", path)
        return {}


def for_debtor(debtor_id: str) -> tuple[str | None, str | None]:
    """(email, phone) for one debtor. Either may be None, and None means ask a human."""
    entry = _load().get(debtor_id) or {}
    email = (entry.get("email") or "").strip() or None
    phone = (entry.get("phone") or "").strip() or None
    return email, phone


def resolve_delivery(intended_email: str) -> tuple[str, str | None]:
    """The address to actually write to, and the intended one when it was overridden.

    Returns `(delivery_address, redirected_from)`. `redirected_from` is None when the two
    are the same, and otherwise names the address the message was really for, so the
    redirect is visible in the message and in the audit row rather than being silent.
    """
    allowed = allowed_recipient()
    if not allowed or intended_email.strip().lower() == allowed.strip().lower():
        return intended_email, None
    return allowed, intended_email
