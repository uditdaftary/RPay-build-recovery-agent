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
from enum import StrEnum
from typing import Any

from app.ledger import InvoiceState, Merchant, UdyamActivity, msmed_eligible


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


ALL_STRATEGIES = frozenset(Strategy)


@dataclass
class EnvelopeResult:
    permitted_strategies: list[Strategy]
    action_classes: dict[Strategy, ActionClass]
    excluded_reasons: dict[Strategy, str]
    max_concession_pct: float
    is_msme_eligible: bool
    msme_eligibility_reason: str


def evaluate_envelope(
    debtor: dict[str, Any],
    invoices: list[dict[str, Any]],
    merchant: Merchant | dict[str, Any],
    *,
    max_concession_pct: float = 0.05,
    vip_exposure_ratio: float = 0.05,
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

    # 1. OPT-OUT: Absolute permanent suppression
    if debtor.get("opted_out", False):
        for s in ALL_STRATEGIES:
            if s != Strategy.WAIT:
                excluded[s] = "debtor has permanently opted out of recovery communications"
                action_classes[s] = ActionClass.PROHIBITED
        return EnvelopeResult(
            permitted_strategies=[Strategy.WAIT],
            action_classes=action_classes,
            excluded_reasons=excluded,
            max_concession_pct=0.0,
            is_msme_eligible=is_eligible,
            msme_eligibility_reason=eligibility_reason,
        )

    # Calculate aggregate invoice statistics
    total_amount_paise = sum(i["amount_paise"] for i in invoices)
    total_received_paise = sum(i.get("amount_received_paise", 0) for i in invoices)
    total_overdue_paise = total_amount_paise - total_received_paise

    has_disputed = any(i.get("state") == InvoiceState.DISPUTED for i in invoices)
    has_tds = any(i.get("state") == InvoiceState.TDS_UNDERPAID for i in invoices)
    has_off_rail = any(i.get("state") == InvoiceState.PAID_OFF_RAIL for i in invoices)
    has_partially_paid = any(i.get("state") == InvoiceState.PARTIALLY_PAID for i in invoices)
    max_days_overdue = max((i.get("days_overdue", 0) for i in invoices), default=0)

    # 2. DISPUTE: Open disputes require resolution or human review, not aggressive chasing
    if has_disputed:
        excluded[Strategy.REQUEST_PAYMENT] = "open invoice dispute requires resolution before chasing payment"
        action_classes[Strategy.REQUEST_PAYMENT] = ActionClass.PROHIBITED
        excluded[Strategy.ESCALATE] = "cannot escalate an account with an active open dispute"
        action_classes[Strategy.ESCALATE] = ActionClass.PROHIBITED
        # RESOLVE_DISPUTE and HUMAN_HANDOFF remain permitted

    # 3. STATUTORY ESCALATION: Gated by MSMED eligibility
    if not is_eligible:
        excluded[Strategy.ESCALATE] = f"statutory escalation unavailable: {eligibility_reason}"
        action_classes[Strategy.ESCALATE] = ActionClass.PROHIBITED

    # 4. RECONCILIATION: Only permitted if TDS discrepancy or off-rail settlement exists
    if not (has_tds or has_off_rail):
        excluded[Strategy.RECONCILE] = "no TDS discrepancy or off-rail payment detected on open invoices"
        action_classes[Strategy.RECONCILE] = ActionClass.PROHIBITED

    # 5. DISPUTE RESOLUTION: Only permitted if an invoice is currently disputed
    if not has_disputed:
        excluded[Strategy.RESOLVE_DISPUTE] = "no disputed invoices on account"
        action_classes[Strategy.RESOLVE_DISPUTE] = ActionClass.PROHIBITED

    # 6. VIP RELATIONSHIP PROTECTION:
    # If exposure is small relative to trailing-12-month value, direct escalation is prohibited
    ttm_value = debtor.get("trailing_12m_value_paise", 0)
    if ttm_value > 0 and (total_overdue_paise / ttm_value) < vip_exposure_ratio and max_days_overdue < 45:
        if Strategy.ESCALATE not in excluded:
            excluded[Strategy.ESCALATE] = (
                f"exposure (Rs {total_overdue_paise // 100:,}) is under {vip_exposure_ratio:.0%} "
                f"of trailing 12M value (Rs {ttm_value // 100:,}); escalation prohibited to preserve relationship"
            )
            action_classes[Strategy.ESCALATE] = ActionClass.PROHIBITED

    # 7. PARTIAL NEGOTIATION: Permitted if partial payment history or substantial overdue amount
    if total_overdue_paise < 50_000_00 and not has_partially_paid:
        excluded[Strategy.NEGOTIATE_PARTIAL] = "invoice balance too small for concession negotiation"
        action_classes[Strategy.NEGOTIATE_PARTIAL] = ActionClass.PROHIBITED

    # Determine final permitted strategies preserving ordering
    permitted = [s for s in Strategy if s not in excluded]

    # Safety guarantee: WAIT and HUMAN_HANDOFF are always safe fallbacks
    if not permitted:
        permitted = [Strategy.WAIT, Strategy.HUMAN_HANDOFF]

    return EnvelopeResult(
        permitted_strategies=permitted,
        action_classes=action_classes,
        excluded_reasons=excluded,
        max_concession_pct=max_concession_pct,
        is_msme_eligible=is_eligible,
        msme_eligibility_reason=eligibility_reason,
    )
