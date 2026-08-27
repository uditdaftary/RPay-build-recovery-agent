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

import logging
from datetime import date, datetime
from typing import Any

from app import audit
from app.envelope import Channel, DebtorHistory, Strategy

logger = logging.getLogger(__name__)


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).date()
    except ValueError:
        return None


def build(
    as_of: date,
    events: list[dict[str, Any]] | None = None,
    *,
    only_debtor: str | None = None,
) -> dict[str, DebtorHistory]:
    """Fold the audit log into per-debtor history as at `as_of`.

    `events` is injectable so the guardrails can be checked against a hand-built log
    without writing files. Passing None reads the real one. `only_debtor` narrows the fold
    to one debtor, for the single-decision path that would otherwise pay to fold the whole
    book and then discard all but one entry.
    """
    if events is None:
        events = audit.read_all()

    last_contact: dict[str, date] = {}
    escalations: dict[str, int] = {}
    promises: dict[str, tuple[date, int | None]] = {}
    undated = 0
    unparsable_promise_dates: list[str] = []

    for event in events:
        debtor_id = event.get("debtor_id")
        if not debtor_id or (only_debtor is not None and debtor_id != only_debtor):
            continue

        # One cutoff, applied before any branch. Every fold below is "as at `as_of`", and
        # a run pinned to a fixed date must not be moved by an event stamped after it.
        # An event we cannot place in time is dropped rather than counted, because
        # counting it would silently move whichever fold it lands in.
        stamped = _parse_date(event.get("ts"))
        if stamped is None:
            undated += 1
            continue
        if stamped > as_of:
            continue

        name = event.get("event")

        if name == "decision.made":
            # A decision with channel NONE was a decision not to make contact, so it does
            # not start a cooldown. A row with no channel at all is read the same way:
            # those are legacy suppression records, and treating them as outreach would
            # silence a debtor the agent never wrote to.
            if (event.get("channel") or Channel.NONE) == Channel.NONE:
                continue
            previous = last_contact.get(debtor_id)
            if previous is None or stamped > previous:
                last_contact[debtor_id] = stamped
            if event.get("strategy") == Strategy.ESCALATE:
                escalations[debtor_id] = escalations.get(debtor_id, 0) + 1

        elif name == "promise.made":
            promised = _parse_date(event.get("promised_date"))
            if promised is None:
                unparsable_promise_dates.append(str(event.get("promised_date")))
            else:
                promises[debtor_id] = (promised, event.get("promised_amount_paise"))

        elif name == "settlement.confirmed":
            # Settlement closes the promise. This is the same suppression the webhook
            # already performs on the ladder, applied to the envelope's view of it.
            promises.pop(debtor_id, None)

    # Warned, never recorded. This function reads the audit log on every decision, so an
    # audit.record here would append a row to the very file being folded — one new row per
    # read, for as long as the bad row exists, in a file that is itself an input to the
    # envelope. Malformed rows are a defect in whatever wrote them, and the warning belongs
    # where defects go rather than in the evaluation artifact.
    if undated:
        logger.warning(
            "contact history skipped %d undated event(s) as of %s", undated, as_of.isoformat()
        )
    if unparsable_promise_dates:
        logger.warning(
            "contact history skipped %d unparsable promise date(s), e.g. %s",
            len(unparsable_promise_dates),
            unparsable_promise_dates[:3],
        )

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
