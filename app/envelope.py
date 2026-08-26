"""Hard policy envelope for the recovery agent.

Stage 1 of the decision engine. Deterministic, ~50 lines.

Given debtor state (agent view only), open invoices, and merchant metadata, the envelope
returns the permitted action set, action classifications (AUTOMATABLE, REVIEW_REQUIRED,
PROHIBITED), and the exact reason each excluded action is excluded.

Built strictly to Razorpay's published agent design principles:
- Agents pick from pre-authorised offers; cannot exceed merchant-defined ceilings.
- Review-first mode (REVIEW_REQUIRED) for high-stakes or irreversible actions.
- Permanent suppression for opt-outs ("a no is a no").
- Strict relationship protection for high-value accounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from app.ledger import InvoiceState, Merchant, msmed_eligible


class Strategy(StrEnum):
    WAIT = "WAIT"
    REQUEST_PAYMENT = "REQUEST_PAYMENT"
    OBTAIN_PROMISE = "OBTAIN_PROMISE"
    NEGOTIATE_PARTIAL = "NEGOTIATE_PARTIAL"
    RESOLVE_DISPUTE = "RESOLVE_DISPUTE"
    RECONCILE = "RECONCILE"
    ESCALATE = "ESCALATE"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"


class ActionClass(StrEnum):
    AUTOMATABLE = "AUTOMATABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"  # Razorpay's "review-first mode"
    PROHIBITED = "PROHIBITED"


# Delivery vocabulary. It lives here beside Strategy and ActionClass rather than in the
# strategist because more than one module has to reason about it: the strategist emits a
# channel, and contact_history has to recognise Channel.NONE to know a decision was a
# decision NOT to make contact. Kept in the strategist, that second reader could only
# restate the values as string literals, which is how "None" once slipped past "none".
class Channel(StrEnum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    PORTAL = "portal"
    NONE = "none"


class Language(StrEnum):
    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


class Tone(StrEnum):
    COLLABORATIVE = "collaborative"
    FIRM = "firm"
    FORMAL = "formal"
    CONCILIATORY = "conciliatory"
    NEUTRAL = "neutral"


ALL_STRATEGIES = frozenset(Strategy)

# Strategies that put a number in front of the debtor. These are the ones the envelope
# forbids outright when nothing is collectible, and the ones whose `ask_amount_paise` the
# strategist must clamp into the pre-authorised band. Keyed on strategy rather than on the
# sign of the ask, so an ask of 0 or None cannot pass as "no ask at all".
ASKS_FOR_MONEY = frozenset(
    {
        Strategy.REQUEST_PAYMENT,
        Strategy.OBTAIN_PROMISE,
        Strategy.NEGOTIATE_PARTIAL,
        Strategy.ESCALATE,
    }
)


@dataclass(frozen=True)
class DebtorHistory:
    """What the agent has already done to this debtor, and what it is waiting on.

    Running state, not seeded state: the ledger is the starting position of the
    experiment, so none of this belongs in it. `app/contact_history.py` derives these from
    the append-only audit log. The default is the honest zero — a debtor never contacted,
    with no promise outstanding — which is what a first run genuinely faces.
    """

    days_since_last_contact: int | None = None
    escalations_sent: int = 0
    active_promise_date: date | None = None
    active_promise_amount_paise: int | None = None


NO_HISTORY = DebtorHistory()


@dataclass
class EnvelopeResult:
    permitted_strategies: list[Strategy]
    action_classes: dict[Strategy, ActionClass]
    excluded_reasons: dict[Strategy, str]
    max_concession_pct: float
    is_msme_eligible: bool
    msme_eligibility_reason: str
    # What is actually still owed, after crediting TDS withheld and off-rail settlement.
    # The single source of truth for every money gate downstream, including the
    # concession floor the strategist clamps against.
    collectible_paise: int


def _exclude(
    excluded: dict[Strategy, str],
    action_classes: dict[Strategy, ActionClass],
    strategy: Strategy,
    reason: str,
) -> None:
    """Record an exclusion, preserving any ground already established.

    Two rules can independently forbid the same action — an open dispute and an
    ineligible supplier both block ESCALATE. Overwriting would drop one ground from
    `excluded_reasons`, which feeds both the prompt and the audit log, so a reader would
    conclude the action is unblocked once the surviving ground clears.
    """
    excluded[strategy] = f"{excluded[strategy]}; also {reason}" if strategy in excluded else reason
    action_classes[strategy] = ActionClass.PROHIBITED


def evaluate_envelope(
    debtor: dict[str, Any],
    invoices: list[dict[str, Any]],
    merchant: Merchant | dict[str, Any],
    *,
    max_concession_pct: float = 0.05,
    vip_exposure_ratio: float = 0.05,
    history: DebtorHistory | None = None,
    cooldown_days: int = 7,
    max_escalations: int = 2,
) -> EnvelopeResult:
    """Evaluate deterministic guardrails for a debtor across their open invoices.

    Returns the permitted action set and reasons for every excluded action.
    """
    if isinstance(merchant, dict):
        merchant_obj = Merchant(
            merchant_id=merchant["merchant_id"],
            name=merchant["name"],
            udyam_registered=merchant["udyam_registered"],
            udyam_category=merchant.get("udyam_category"),
            udyam_activity=merchant.get("udyam_activity"),
        )
    else:
        merchant_obj = merchant

    is_eligible, eligibility_reason = msmed_eligible(merchant_obj)

    excluded: dict[Strategy, str] = {}
    action_classes: dict[Strategy, ActionClass] = {s: ActionClass.AUTOMATABLE for s in ALL_STRATEGIES}

    # High-stakes actions default to REVIEW_REQUIRED per Razorpay design principles
    action_classes[Strategy.ESCALATE] = ActionClass.REVIEW_REQUIRED
    action_classes[Strategy.HUMAN_HANDOFF] = ActionClass.REVIEW_REQUIRED

    # Calculate aggregate invoice statistics.
    # TDS withheld is remitted to the exchequer on the supplier's behalf, so the buyer
    # who deducted it has paid in full; an off-rail NEFT settlement is likewise already
    # collected. Neither is a shortfall, so neither may drive a payment demand, a
    # concession, or an exposure calculation. `outstanding_paise` on the invoice is the
    # naive figure by design; this is the collectible one.
    total_amount_paise = sum(i["amount_paise"] for i in invoices)
    total_received_paise = sum(i.get("amount_received_paise", 0) for i in invoices)
    total_tds_paise = sum(i.get("tds_deducted_paise", 0) for i in invoices)
    collectible_paise = total_amount_paise - total_received_paise - total_tds_paise

    # 1. OPT-OUT: Absolute permanent suppression.
    # Fails CLOSED. An absent flag is not consent: a partial debtor row must never silently
    # resume chasing someone who opted out. "No exceptions, no 'just one more try.' A no is
    # a no." This is the most absolute rule in the envelope, so it gets the safest default.
    opted_out = debtor.get("opted_out")
    if opted_out is None or opted_out:
        for s in ALL_STRATEGIES:
            if s != Strategy.WAIT:
                _exclude(
                    excluded,
                    action_classes,
                    s,
                    "debtor has permanently opted out of recovery communications",
                )
        return EnvelopeResult(
            permitted_strategies=[Strategy.WAIT],
            action_classes=action_classes,
            excluded_reasons=excluded,
            max_concession_pct=0.0,
            is_msme_eligible=is_eligible,
            msme_eligibility_reason=eligibility_reason,
            collectible_paise=collectible_paise,
        )

    has_disputed = any(i.get("state") == InvoiceState.DISPUTED for i in invoices)
    has_tds = any(i.get("state") == InvoiceState.TDS_UNDERPAID for i in invoices)
    has_off_rail = any(i.get("state") == InvoiceState.PAID_OFF_RAIL for i in invoices)
    has_partially_paid = any(i.get("state") == InvoiceState.PARTIALLY_PAID for i in invoices)
    max_days_overdue = max((i.get("days_overdue", 0) for i in invoices), default=0)

    # 2. DISPUTE: Open disputes require resolution or human review, not aggressive chasing
    if has_disputed:
        _exclude(
            excluded,
            action_classes,
            Strategy.REQUEST_PAYMENT,
            "open invoice dispute requires resolution before chasing payment",
        )
        _exclude(
            excluded,
            action_classes,
            Strategy.ESCALATE,
            "cannot escalate an account with an active open dispute",
        )
        # RESOLVE_DISPUTE and HUMAN_HANDOFF remain permitted

    # 2b. NOTHING COLLECTIBLE: every open invoice is already settled, by TDS credit or by
    # off-rail NEFT. Asking for money that has been paid is the naive-agent failure the
    # PAID_OFF_RAIL and TDS_UNDERPAID states exist to catch, so the envelope forbids it
    # by construction rather than relying on the prompt to discourage it. RECONCILE stays
    # open — chasing the UTR or the Form 26AS is exactly the right move here.
    if collectible_paise <= 0:
        for money_ask in sorted(ASKS_FOR_MONEY):
            _exclude(
                excluded,
                action_classes,
                money_ask,
                "no collectible balance: every open invoice reconciles to face value once "
                "TDS credit and off-rail settlement are applied",
            )

    # 3. STATUTORY ESCALATION: Gated by MSMED eligibility
    if not is_eligible:
        _exclude(
            excluded,
            action_classes,
            Strategy.ESCALATE,
            f"statutory escalation unavailable: {eligibility_reason}",
        )

    # 4. RECONCILIATION: Only permitted if TDS discrepancy or off-rail settlement exists
    if not (has_tds or has_off_rail):
        _exclude(
            excluded,
            action_classes,
            Strategy.RECONCILE,
            "no TDS discrepancy or off-rail payment detected on open invoices",
        )

    # 5. DISPUTE RESOLUTION: Only permitted if an invoice is currently disputed
    if not has_disputed:
        _exclude(
            excluded, action_classes, Strategy.RESOLVE_DISPUTE, "no disputed invoices on account"
        )

    # 6. VIP RELATIONSHIP PROTECTION:
    # If exposure is small relative to trailing-12-month value, direct escalation is
    # prohibited. Unknown account value fails CLOSED: an account we cannot size is treated
    # as one worth protecting, because the cost of a wrong escalation is the relationship.
    # Skipped when nothing is collectible: a zero balance satisfies the exposure ratio
    # trivially, and recording a relationship-protection ground the agent never weighed
    # would misrepresent the reasoning in the audit trail. Rule 2b already covers that case.
    ttm_value = debtor.get("trailing_12m_value_paise") or 0
    if max_days_overdue < 45 and collectible_paise > 0:
        if ttm_value <= 0:
            _exclude(
                excluded,
                action_classes,
                Strategy.ESCALATE,
                "trailing 12M account value unknown, so exposure cannot be shown to justify "
                "escalation; protecting the relationship by default",
            )
        elif (collectible_paise / ttm_value) < vip_exposure_ratio:
            _exclude(
                excluded,
                action_classes,
                Strategy.ESCALATE,
                f"exposure (Rs {collectible_paise // 100:,}) is under {vip_exposure_ratio:.0%} "
                f"of trailing 12M value (Rs {ttm_value // 100:,}); escalation prohibited to "
                "preserve relationship",
            )

    # 7. PARTIAL NEGOTIATION: Permitted if partial payment history or substantial balance
    if collectible_paise < 50_000_00 and not has_partially_paid:
        _exclude(
            excluded,
            action_classes,
            Strategy.NEGOTIATE_PARTIAL,
            "invoice balance too small for concession negotiation",
        )

    # 8-10. WHAT THE AGENT HAS ALREADY DONE. These three read running state rather than
    # invoice state, so they only bite once the agent has a history with this debtor.
    # All of them gate the money asks and leave RECONCILE, RESOLVE_DISPUTE and
    # HUMAN_HANDOFF open: those respond to a state the debtor is already in, rather than
    # chasing, and silencing them would trap an account that needs exactly that response.
    past = history or NO_HISTORY

    # 8. CONTACT-FREQUENCY COOLDOWN
    if past.days_since_last_contact is not None and past.days_since_last_contact < cooldown_days:
        for money_ask in sorted(ASKS_FOR_MONEY):
            _exclude(
                excluded,
                action_classes,
                money_ask,
                f"last contacted {past.days_since_last_contact} day(s) ago; the "
                f"{cooldown_days}-day contact cooldown has not elapsed",
            )

    # 9. MAX INTENSITY REACHED
    if past.escalations_sent >= max_escalations:
        _exclude(
            excluded,
            action_classes,
            Strategy.ESCALATE,
            f"{past.escalations_sent} escalations already sent, which is the ceiling of "
            f"{max_escalations}; further intensity is a human's decision, not the agent's",
        )

    # 10. ACTIVE PROMISE RUNNING
    # A debtor who committed to a date has not broken anything yet. Chasing inside their
    # own promise is the fastest way to lose the relationship the promise just bought.
    if past.active_promise_date is not None:
        committed = (
            f" for {past.active_promise_amount_paise // 100:,} rupees"
            if past.active_promise_amount_paise
            else ""
        )
        for money_ask in sorted(ASKS_FOR_MONEY):
            _exclude(
                excluded,
                action_classes,
                money_ask,
                f"debtor has an open promise to pay{committed} by "
                f"{past.active_promise_date.isoformat()}, which has not fallen due",
            )

    # Determine final permitted strategies preserving ordering. WAIT is never excluded by
    # any rule above, so this is never empty.
    permitted = [s for s in Strategy if s not in excluded]

    return EnvelopeResult(
        permitted_strategies=permitted,
        action_classes=action_classes,
        excluded_reasons=excluded,
        max_concession_pct=max_concession_pct,
        is_msme_eligible=is_eligible,
        msme_eligibility_reason=eligibility_reason,
        collectible_paise=collectible_paise,
    )
