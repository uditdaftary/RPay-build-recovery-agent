"""Contact and promise history, derived from the append-only audit log.

Three envelope rules need to know what the agent has already done to a debtor: whether it
contacted them recently, how far up the intensity ladder it has already gone, and whether
the debtor is inside a promise they have not yet broken.

None of that belongs in the seeded ledger. The ledger is the starting position of the
experiment and has to stay byte-identical from its seed; running state that changes every
time the agent acts would destroy that. Nor does it want a second store kept in sync by
hand. The audit log already records every decision the strategist made, every promise the
resolution page captured, and every settlement the webhook confirmed, so this derives the
history from that one source rather than inventing another.

Reading the log is the reason this lives outside `envelope.py`: the envelope stays a pure
function of the state it is handed, which is what makes its guardrails testable without
touching a file.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app import audit
from app.envelope import DebtorHistory, Strategy

# Events that mean the agent put a message in front of this debtor. A decision with
# channel "none" was a decision not to make contact, so it does not start a cooldown.
SILENT_CHANNELS = frozenset({"", "none"})


def _event_date(event: dict[str, Any]) -> date | None:
    raw = event.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        # A malformed timestamp must not silently read as "never contacted", which would
        # fail open on the cooldown. Drop the event and say so.
        audit.record("contact_history.unparsable_timestamp", ts=raw, source_event=event.get("event"))
        return None


def build(
    as_of: date, events: list[dict[str, Any]] | None = None
) -> dict[str, DebtorHistory]:
    """Fold the audit log into per-debtor history as at `as_of`.

    `events` is injectable so the guardrails can be checked against a hand-built log
    without writing files. Passing None reads the real one.
    """
    if events is None:
        events = audit.read_all()

    last_contact: dict[str, date] = {}
    escalations: dict[str, int] = {}
    promises: dict[str, tuple[date, int | None]] = {}

    for event in events:
        debtor_id = event.get("debtor_id")
        if not debtor_id:
            continue
        name = event.get("event")

        if name == "decision.made":
            if str(event.get("channel", "")) in SILENT_CHANNELS:
                continue
            stamped = _event_date(event)
            if stamped is not None and stamped <= as_of:
                previous = last_contact.get(debtor_id)
                if previous is None or stamped > previous:
                    last_contact[debtor_id] = stamped
            if event.get("strategy") == Strategy.ESCALATE:
                escalations[debtor_id] = escalations.get(debtor_id, 0) + 1

        elif name == "promise.made":
            promised = event.get("promised_date")
            if promised:
                try:
                    promises[debtor_id] = (
                        date.fromisoformat(promised),
                        event.get("promised_amount_paise"),
                    )
                except ValueError:
                    audit.record(
                        "contact_history.unparsable_promise_date",
                        debtor_id=debtor_id,
                        promised_date=promised,
                    )

        elif name == "settlement.confirmed":
            # Settlement closes the promise. This is the same suppression the webhook
            # already performs on the ladder, applied to the envelope's view of it.
            promises.pop(debtor_id, None)

    histories: dict[str, DebtorHistory] = {}
    for debtor_id in set(last_contact) | set(escalations) | set(promises):
        promised = promises.get(debtor_id)
        # A promise whose date has passed without settlement is broken, not active, and a
        # broken promise must not go on shielding the debtor from being chased.
        active = promised if promised and promised[0] >= as_of else None
        contacted = last_contact.get(debtor_id)
        histories[debtor_id] = DebtorHistory(
            days_since_last_contact=(as_of - contacted).days if contacted else None,
            escalations_sent=escalations.get(debtor_id, 0),
            active_promise_date=active[0] if active else None,
            active_promise_amount_paise=active[1] if active else None,
        )
    return histories
