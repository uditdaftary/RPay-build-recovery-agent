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
from app.config import BUSINESS_TZ, business_today
from app.envelope import Channel, DebtorHistory, Strategy
from app.ledger import InvoiceState
from app.ledger import balance_paise as _balance_paise

logger = logging.getLogger(__name__)

# Audit events that credit money against an invoice. Both raise `amount_received_paise` in
# the projection below; the only difference is which one the webhook writes.
SETTLEMENT_EVENTS = frozenset({"settlement.confirmed", "settlement.partial"})

# How many separate suppression windows one debtor may open without a payment landing in
# between. `/api/promise` is public and unauthenticated, and an open promise excludes every
# money ask, so without this a debtor keeps recovery switched off by promising again each
# time the last date passes. Two broken commitments is already the pattern; the third is not
# honoured and the agent resumes. A settlement resets the count, because money landing is the
# thing the promise was for.
MAX_PROMISE_WINDOWS_WITHOUT_SETTLEMENT = 2


def _parse_date(raw: Any) -> date | None:
    """Read a stamp as an Indian business date. Naive values are already local.

    The audit log stamps UTC, and every date in the ledger is a business date, so taking
    .date() off a UTC instant put everything logged between 00:00 and 05:30 IST on the
    previous business day and lifted the cooldown a day early.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return (parsed.astimezone(BUSINESS_TZ) if parsed.tzinfo else parsed).date()


def history_as_of(ledger_as_of: date, today: date | None = None) -> date:
    """The date the audit fold is taken at, given the ledger's pinned `as_of`.

    Two different clocks meet here. Invoice ageing is pinned to the ledger's `as_of` so the
    experiment is reproducible, but the resolution page stamps a promise, a dispute or a
    settlement with the wall clock. Folding real events at a pinned date in the past means
    `build`'s own cutoff discards every one of them, so a debtor who commits to a payment
    date is invisible to the cooldown, escalation and open-promise rules — the end-to-end
    "debtor promises, agent stops chasing" path could never fire once the wall clock passed
    the seeded date, which it already had.

    The later of the two: the ledger's date while the experiment is still in its own present,
    the real date once the world has moved past it.

    `today` is injectable because this is the one place a decision batch stops being a pure
    function of its seed. A caller that needs a run pinned - a test, or a demo that must
    reproduce a recorded result - passes the date rather than monkeypatching the clock.
    """
    return max(ledger_as_of, today or business_today())


def live_invoice_state(
    invoices: list[dict[str, Any]], events: list[dict[str, Any]], as_of: date
) -> list[dict[str, Any]]:
    """Project settlements from the audit log onto a copy of the invoice list.

    The envelope reads `collectible = amount - amount_received - tds` per invoice, so a
    settlement credited here raises `amount_received_paise` and the paid money leaves every
    downstream gate: a full payment drops the invoice out of collectible entirely, a partial
    leaves the remainder chaseable and marks the invoice PARTIALLY_PAID so the negotiation
    rule still applies to it.

    Never mutates its input. The seeded ledger is the experiment's starting position and has
    to stay byte-identical, so this returns fresh dicts for the invoices it touches and the
    originals untouched for the rest.

    This is the one call that makes a decision depend on wall-clock settlements rather than
    the seed, which is why `run_strategist_batch` keeps it off by default and only the demo
    turns it on. Folding it into the experiment would make the uplift number a function of
    the audit log's contents. Dedup is on `payment_id` because Razorpay redelivers webhooks
    and a replayed partial would otherwise be counted twice.
    """
    settled: dict[str, int] = {}
    seen: set[str] = set()
    unparsable_amounts = 0
    for event in events:
        if event.get("event") not in SETTLEMENT_EVENTS:
            continue
        invoice_id = event.get("invoice_id")
        if not invoice_id:
            continue
        stamped = _parse_date(event.get("ts"))
        if stamped is None or stamped > as_of:
            continue
        payment_id = event.get("payment_id")
        if payment_id is not None:
            key = f"{invoice_id}:{payment_id}"
            if key in seen:
                continue
            seen.add(key)
        # A present-but-null or non-numeric amount is a defect in whatever wrote the row,
        # same as an unparsable date or promise amount elsewhere in this file: skipped and
        # counted, not raised, because one bad row must not take the whole fold down with it.
        raw_amount = event.get("amount_paise")
        try:
            credited_amount = int(raw_amount) if raw_amount is not None else 0
        except (TypeError, ValueError):
            unparsable_amounts += 1
            continue
        settled[invoice_id] = settled.get(invoice_id, 0) + credited_amount

    if unparsable_amounts:
        logger.warning(
            "live invoice state skipped %d settlement event(s) with an unparsable amount_paise",
            unparsable_amounts,
        )

    projected: list[dict[str, Any]] = []
    for invoice in invoices:
        credited = settled.get(invoice["invoice_id"], 0)
        if not credited:
            projected.append(invoice)
            continue
        updated = dict(invoice)
        updated["amount_received_paise"] = invoice.get("amount_received_paise", 0) + credited
        remaining = _balance_paise(updated)
        updated["state"] = str(
            InvoiceState.PAID if remaining <= 0 else InvoiceState.PARTIALLY_PAID
        )
        projected.append(updated)
    return projected


def build(
    as_of: date,
    events: list[dict[str, Any]] | None = None,
    *,
    only_debtor: str | None = None,
) -> dict[str, DebtorHistory]:
    """Fold the audit log into per-debtor history as at `as_of`.

    `events` is injectable so the guardrails can be checked against a hand-built log
    without writing files. Passing None reads the real one.

    `only_debtor` narrows the fold, not the read: the whole log is still parsed, because a
    JSONL file cannot be seeked by debtor. It saves the folding, not the I/O, so a caller
    deciding for many debtors must build the map once and pass it in rather than calling
    this per debtor. `run_strategist_batch` does exactly that.
    """
    if events is None:
        events = audit.read_all()

    last_contact: dict[str, date] = {}
    escalations: dict[str, int] = {}
    promises: dict[str, tuple[date, int | None, str | None]] = {}
    promise_windows: dict[str, int] = {}
    undated = 0
    unparsable_promise_dates: list[str] = []
    extensions_refused = 0
    windows_exhausted: set[str] = set()

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
                continue

            # A promise excludes every money ask until its date falls due, so the number of
            # them a debtor may make is the real bound on how long recovery can be held off.
            # The 90-day horizon on any single promise only made an unbounded hold periodic:
            # one request every 89 days, or one the day after each broken date, suppressed
            # the account for as long as the debtor cared to keep asking. Two rules bound it.
            open_promise = promises.get(debtor_id)
            still_open = open_promise is not None and open_promise[0] >= stamped

            if still_open:
                # A commitment already running may be brought forward, never pushed back.
                # Moving the date out is the extension move, and it is the whole attack.
                # Bringing it in is a debtor offering to pay sooner, which costs nothing and
                # does not open a new window, so it is not counted below.
                if promised > open_promise[0]:
                    extensions_refused += 1
                    continue
            else:
                # No promise running, so this one opens a fresh suppression window. Count it.
                # Windows, not promises: a bring-forward inside a window is free, and only a
                # date the debtor let pass without paying gets them to the next one.
                windows = promise_windows.get(debtor_id, 0) + 1
                promise_windows[debtor_id] = windows
                if windows > MAX_PROMISE_WINDOWS_WITHOUT_SETTLEMENT:
                    windows_exhausted.add(debtor_id)
                    continue

            promises[debtor_id] = (
                promised,
                event.get("promised_amount_paise"),
                event.get("invoice_id"),
            )

        elif name in SETTLEMENT_EVENTS:
            # Settlement closes the promise — a partial capture counts too, the same as a
            # full one: money landing is the thing the promise was for, whether or not it
            # clears the balance. This is the same suppression the webhook already performs
            # on the ladder, applied to the envelope's view of it.
            #
            # Only the invoice the promise was made on closes it. Both rows are keyed on
            # the debtor, so paying invoice B used to cancel an open promise running on
            # invoice A and resume chasing a debtor who had broken nothing. A promise with
            # no invoice recorded is closed by any settlement, which is the old behaviour.
            open_promise = promises.get(debtor_id)
            if open_promise is not None and open_promise[2] in (None, event.get("invoice_id")):
                promises.pop(debtor_id, None)
            # The escalation count is deliberately NOT reset here. `decision.made` carries no
            # invoice, so any reset can only be debtor-wide, and a small payment on invoice E
            # would refill the ceiling the agent had already spent on invoice A - a safety
            # limit that an unrelated event tops up is worse than one that never resets. The
            # cost is that a debtor who once reached the ceiling stays on HUMAN_HANDOFF for
            # escalation, which is a human deciding rather than the agent escalating.
            # Revisit when a decision row can name the invoice or cycle it belongs to.
            #
            # The promise budget IS reset, and the asymmetry is the point: the escalation
            # ceiling protects the debtor from the agent, so refilling it does harm, while
            # the promise budget protects recovery from the debtor, and a debtor who has
            # actually paid has earned the right to commit again.
            promise_windows[debtor_id] = 0

    # Warned, never recorded. This function reads the audit log on every decision, so an
    # audit.record here would append a row to the very file being folded — one new row per
    # read, for as long as the bad row exists, in a file that is itself an input to the
    # envelope. Malformed rows are a defect in whatever wrote them, and the warning belongs
    # where defects go rather than in the evaluation artifact.
    if undated:
        logger.warning(
            "contact history skipped %d undated event(s) as of %s", undated, as_of.isoformat()
        )
    if extensions_refused or windows_exhausted:
        logger.warning(
            "contact history ignored %d promise extension(s) and stopped honouring promises "
            "for %s after %d window(s) without a settlement",
            extensions_refused,
            sorted(windows_exhausted) or "nobody",
            MAX_PROMISE_WINDOWS_WITHOUT_SETTLEMENT,
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
