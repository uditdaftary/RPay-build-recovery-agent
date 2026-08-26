"""AI Recovery Strategist for B2B Receivables.

Stage 2 of the decision engine.
Given debtor state, open invoices, and the hard policy envelope, the strategist selects
the optimal recovery intervention. Emits structured decisions with reasoning, confidence,
and documented rejected alternatives.

Key tenets:
1. Operates PER DEBTOR, not per invoice (multi-invoice aggregation).
2. Constrained by the Hard Policy Envelope (app/envelope.py).
3. WAIT is a first-class strategy for reliable late payers (restraint over noise).
4. India-specific nuance: TDS reconciliation, UTR checks, MSMED refusal.
5. No dark patterns: no false urgency, no confirm shaming, no manufactured scarcity.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app import audit, contact_history, llm
from app.envelope import (
    ASKS_FOR_MONEY,
    NO_HISTORY,
    ActionClass,
    DebtorHistory,
    EnvelopeResult,
    Strategy,
    evaluate_envelope,
)
from app.ledger import agent_view


# The envelope hardens which strategy may run; these harden how it reaches the debtor.
# Enumerated for the same reason Strategy and ActionClass are: a member the model invented
# must fail validation rather than reach a dispatcher that has no branch for it.
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


class RejectedAction(BaseModel):
    strategy: Strategy
    reason: str


class StrategistDecision(BaseModel):
    debtor_id: str
    debtor_name: str
    strategy: Strategy
    channel: Channel
    language: Language
    tone: Tone
    ask_amount_paise: int | None = None
    # A real date, not prose. The promise tracker auto-checks this on the day it names, so
    # "next Tuesday" has to fail here rather than on the day it was supposed to fire.
    deadline_requested: date | None = None
    reasoning: str
    rejected_actions: list[RejectedAction] = []
    confidence: float = Field(ge=0.0, le=1.0)
    review_required: bool = False
    action_class: ActionClass = ActionClass.AUTOMATABLE


SYSTEM_PROMPT = """You are an expert B2B Receivables Recovery Strategist for Indian MSMEs.
Your goal is to maximize long-term cash collection while strictly protecting merchant-buyer relationships.

Core rules and principles:
1. DECIDE PER DEBTOR: You are reviewing a debtor who may have multiple open invoices. Formulate one unified intervention.
2. HARD ENVELOPE BOUNDARY: You MUST choose your `strategy` ONLY from the list of `permitted_strategies` provided in the prompt. Choosing an excluded strategy violates policy.
3. RESTRAINT IS FIRST-CLASS: If the debtor habitually pays late but reliably (high promise kept rate, known average days late), or if recent outreach occurred, choose `WAIT`. Knowing when NOT to chase is superior to automated spam.
4. INDIA-SPECIFIC RECONCILIATION:
   - If an invoice is `TDS_UNDERPAID`, the buyer legitimately withheld TDS. Choose `RECONCILE` to request Form 26AS/TDS certificate. NEVER accuse them of underpayment.
   - If an invoice is `PAID_OFF_RAIL`, choose `RECONCILE` to verify the bank NEFT/RTGS UTR reference.
   - If an invoice is `DISPUTED`, choose `RESOLVE_DISPUTE` or `HUMAN_HANDOFF` to gather evidence; do not demand payment.
   - If statutory escalation is prohibited (e.g. supplier is a trader or non-micro/small), do NOT threaten legal action or Section 15/16 interest.
5. NO DARK PATTERNS: Do not manufacture false urgency, confirm-shame, or use manipulative copy. Maintain professional, commercial B2B tone.
6. DOCUMENT REJECTED ACTIONS: You must populate `rejected_actions` with 1-2 strategies you considered but rejected, giving the specific reason why (proves AI judgment).
7. PRE-AUTHORISED CONCESSIONS ONLY: `ask_amount_paise` must never fall below the `min_ask_paise` given in the envelope. That floor is the merchant's pre-authorised concession ceiling. You may not invent a discount, waive interest, or settle for less; anything lower is clamped and escalated for human review.
8. AMOUNTS ARE COLLECTIBLE AMOUNTS: `total_collectible` already credits TDS withheld and off-rail settlement. Never ask for more than it, and never treat a TDS deduction as a shortfall.
"""


def _format_inr(paise: int) -> str:
    rupees = paise // 100
    return f"Rs {rupees:,}"


def _record_decision(decision: StrategistDecision) -> None:
    """Write `decision.made` in one shape, whatever path produced the decision.

    The suppression fast path used to emit a shorter record than the model path, so the
    opted-out debtor — the row the pitch leans on — was the one missing the action class,
    channel and rejected alternatives the results page reads.
    """
    audit.record(
        "decision.made",
        debtor_id=decision.debtor_id,
        debtor_name=decision.debtor_name,
        strategy=decision.strategy,
        action_class=decision.action_class,
        review_required=decision.review_required,
        channel=decision.channel,
        tone=decision.tone,
        ask_amount_paise=decision.ask_amount_paise,
        deadline_requested=decision.deadline_requested,
        reasoning=decision.reasoning,
        rejected_actions=[r.model_dump() for r in decision.rejected_actions],
    )


def decide_for_debtor(
    debtor: dict[str, Any],
    invoices: list[dict[str, Any]],
    merchant: dict[str, Any],
    *,
    as_of_date: str = "2026-08-26",
    history: DebtorHistory | None = None,
) -> StrategistDecision:
    """Evaluate envelope and call the LLM to generate a recovery decision for a debtor."""
    # Project before anything else reads the debtor. Callers hand us rows straight out of
    # data/ledger.json, which still carry the hidden behaviour parameters, and this is the
    # one chokepoint every decision passes through — so the projection belongs here rather
    # than in each caller, where one forgotten call site would silently invalidate a run.
    debtor = agent_view(debtor)

    debtor_id = debtor.get("debtor_id", "")
    debtor_name = debtor.get("name", "")

    # Cooldown, intensity and open-promise state gate the money asks, so an unsupplied
    # history is derived here rather than defaulting to "no history" — a guard that
    # silently switches itself off for direct callers is not a guard. Batches pass their
    # own prebuilt map so the log is folded once instead of once per debtor.
    if history is None:
        history = contact_history.build(date.fromisoformat(as_of_date)).get(debtor_id, NO_HISTORY)

    envelope: EnvelopeResult = evaluate_envelope(debtor, invoices, merchant, history=history)

    # Fast-path / hard-suppression. Reads the flag directly rather than inferring it from
    # the envelope collapsing to [WAIT]: that coincidence holds only while no rule outside
    # the opt-out block excludes HUMAN_HANDOFF, and the moment one does (a kill switch, a
    # full review queue) a debtor who never opted out would be logged as one.
    if debtor["opted_out"]:
        decision = StrategistDecision(
            debtor_id=debtor_id,
            debtor_name=debtor_name,
            strategy=Strategy.WAIT,
            channel="none",
            language=debtor.get("language", "en"),
            tone="neutral",
            ask_amount_paise=0,
            reasoning=envelope.excluded_reasons.get(
                Strategy.REQUEST_PAYMENT, "Debtor permanently opted out; suppression enforced."
            ),
            rejected_actions=[
                RejectedAction(
                    strategy=s,
                    reason=envelope.excluded_reasons.get(s, "Prohibited by envelope"),
                )
                for s in Strategy
                if s != Strategy.WAIT
            ],
            confidence=1.0,
            review_required=False,
            action_class=ActionClass.AUTOMATABLE,
        )
        _record_decision(decision)
        return decision

    # Build prompt payload. The envelope already netted off TDS credit and off-rail
    # settlement; reuse its figure so the prompt, the concession floor and the money gates
    # cannot drift apart.
    collectible_paise = envelope.collectible_paise

    debtor_context = {
        "debtor_id": debtor_id,
        "name": debtor_name,
        "relationship_since": debtor.get("relationship_since"),
        "trailing_12m_value": _format_inr(debtor.get("trailing_12m_value_paise", 0)),
        "preferred_channel": debtor.get("preferred_channel", "email"),
        "language": debtor.get("language", "en"),
        "promises_kept": f"{debtor.get('promises_kept', 0)} of {debtor.get('promises_made', 0)}",
        "prior_disputes": debtor.get("prior_disputes", 0),
        "avg_days_late": debtor.get("avg_days_late", 0),
        "open_invoices_count": len(invoices),
        "total_collectible": _format_inr(collectible_paise),
    }

    invoice_summaries = [
        {
            "invoice_id": inv["invoice_id"],
            "amount": _format_inr(inv["amount_paise"]),
            "state": inv.get("state", "OVERDUE"),
            "days_overdue": inv.get("days_overdue", 0),
            "due_date": inv.get("contractual_due_date"),
            "tds_deducted": _format_inr(inv.get("tds_deducted_paise", 0)) if inv.get("tds_deducted_paise") else None,
            "off_rail_reference": inv.get("off_rail_reference"),
            "dispute_reason": inv.get("dispute_reason"),
        }
        for inv in invoices
    ]

    merchant_summary = {
        "merchant_id": merchant.get("merchant_id"),
        "name": merchant.get("name"),
        "msme_eligible": envelope.is_msme_eligible,
        "msme_eligibility_reason": envelope.msme_eligibility_reason,
    }

    # The pre-authorised band any ask must land in. Razorpay's principle is that agents
    # pick from pre-authorised offers and cannot exceed merchant-defined ceilings, so both
    # ends are bounds the code enforces below, not numbers the prompt merely mentions.
    # Integer basis points throughout: this decides how much money the agent may forgo, and
    # the rest of the codebase keeps paise exact.
    concession_bps = int(round(envelope.max_concession_pct * 10_000))
    min_ask_paise = collectible_paise - collectible_paise * concession_bps // 10_000

    envelope_summary = {
        "permitted_strategies": [s.value for s in envelope.permitted_strategies],
        "excluded_strategies": {s.value: r for s, r in envelope.excluded_reasons.items()},
        "max_concession_pct": envelope.max_concession_pct,
        "min_ask_paise": min_ask_paise,
    }

    user_prompt = f"""Evaluate debtor for recovery action as of {as_of_date}:

DEBTOR PROFILE:
{json.dumps(debtor_context, indent=2)}

OPEN INVOICES ({len(invoices)}):
{json.dumps(invoice_summaries, indent=2)}

MERCHANT / SUPPLIER:
{json.dumps(merchant_summary, indent=2)}

HARD POLICY ENVELOPE (STRICT BOUNDARIES):
{json.dumps(envelope_summary, indent=2)}

Return your structured decision according to the schema. Ensure strategy is one of: {envelope_summary['permitted_strategies']}.
"""

    raw_json = llm.complete(
        user_prompt,
        system=SYSTEM_PROMPT,
        response_schema=StrategistDecision,
        temperature=0.1,
    )

    try:
        data = json.loads(raw_json)
        # Ensure debtor fields are populated
        data["debtor_id"] = debtor_id
        data["debtor_name"] = debtor_name
        decision = StrategistDecision.model_validate(data)
    except Exception as exc:
        # If response parsing fails, fallback to safe strategy
        fallback_strategy = Strategy.WAIT if Strategy.WAIT in envelope.permitted_strategies else Strategy.HUMAN_HANDOFF
        audit.record(
            "strategist.parse_failed",
            debtor_id=debtor_id,
            error=str(exc),
            fell_back_to=fallback_strategy,
        )
        decision = StrategistDecision(
            debtor_id=debtor_id,
            debtor_name=debtor_name,
            strategy=fallback_strategy,
            channel="none",
            language=debtor.get("language", "en"),
            tone="neutral",
            ask_amount_paise=0,
            reasoning=f"LLM output parsing error ({exc}); fell back to {fallback_strategy.value}.",
            confidence=0.5,
            review_required=True,
            action_class=ActionClass.REVIEW_REQUIRED,
        )

    # Hard guardrail verification: ensure strategy is in permitted set
    if decision.strategy not in envelope.permitted_strategies:
        audit.record(
            "envelope.violation_intercepted",
            debtor_id=debtor_id,
            attempted_strategy=decision.strategy,
            permitted=envelope_summary["permitted_strategies"],
        )
        # Force safe fallback
        decision.strategy = Strategy.WAIT if Strategy.WAIT in envelope.permitted_strategies else Strategy.HUMAN_HANDOFF
        decision.reasoning = (
            f"Policy envelope intercepted prohibited action and defaulted to {decision.strategy}. "
            + decision.reasoning
        )
        decision.review_required = True

    # Hard guardrail verification: a money ask must land inside the pre-authorised band.
    # Below the floor is a concession the merchant never authorised. Above the collectible
    # balance is a demand for money already paid — on a TDS invoice that is exactly the
    # accuse-a-good-buyer failure the TDS_UNDERPAID state exists to catch. Gated on the
    # strategy rather than on the sign of the ask, because 0 and None are the largest
    # possible concession and must not slip through as "no ask at all".
    if decision.strategy in ASKS_FOR_MONEY:
        requested = decision.ask_amount_paise or 0
        bounded = min(max(requested, min_ask_paise), collectible_paise)
        if bounded != requested:
            audit.record(
                "envelope.ask_out_of_band",
                debtor_id=debtor_id,
                strategy=decision.strategy,
                attempted_ask_paise=decision.ask_amount_paise,
                min_ask_paise=min_ask_paise,
                max_ask_paise=collectible_paise,
                max_concession_pct=envelope.max_concession_pct,
            )
            decision.reasoning = (
                f"Policy envelope clamped an ask of Rs {requested // 100:,} into the "
                f"pre-authorised band Rs {min_ask_paise // 100:,} to "
                f"Rs {collectible_paise // 100:,}. " + decision.reasoning
            )
            decision.ask_amount_paise = bounded
            decision.review_required = True
    elif decision.ask_amount_paise:
        # The other half of the same rule. RECONCILE asks for a Form 26AS or a UTR and
        # WAIT asks for nothing at all; an amount riding along on either becomes a demand
        # for money once a decision is rendered into a message.
        audit.record(
            "envelope.ask_on_non_money_strategy",
            debtor_id=debtor_id,
            strategy=decision.strategy,
            attempted_ask_paise=decision.ask_amount_paise,
        )
        decision.reasoning = (
            f"Policy envelope dropped an ask of Rs {decision.ask_amount_paise // 100:,} from a "
            f"{decision.strategy.value} decision, which asks for no money. " + decision.reasoning
        )
        decision.ask_amount_paise = 0
        decision.review_required = True

    # A deadline the promise tracker cannot act on is worse than none: it would be logged
    # as a commitment and then never fire. `as_of_date` is the reference, not the wall
    # clock, because the whole experiment is pinned to the ledger's date.
    if decision.deadline_requested is not None and decision.deadline_requested < date.fromisoformat(as_of_date):
        audit.record(
            "envelope.deadline_in_past",
            debtor_id=debtor_id,
            deadline_requested=decision.deadline_requested,
            as_of=as_of_date,
        )
        decision.reasoning = (
            f"Policy envelope dropped a deadline of {decision.deadline_requested.isoformat()}, "
            f"which is already past as of {as_of_date}. " + decision.reasoning
        )
        decision.deadline_requested = None
        decision.review_required = True

    # Set action class from the envelope. `review_required` is sticky: an interception or a
    # clamp above already demanded human review, and recomputing it from the action class
    # alone would clear that flag whenever the forced fallback happens to be AUTOMATABLE.
    decision.action_class = envelope.action_classes.get(decision.strategy, ActionClass.AUTOMATABLE)
    decision.review_required = (
        decision.review_required or decision.action_class == ActionClass.REVIEW_REQUIRED
    )

    _record_decision(decision)

    return decision


def run_strategist_batch(
    ledger: dict[str, Any], *, limit: int | None = None
) -> list[StrategistDecision]:
    """Run AI recovery strategist over debtors in the ledger."""
    merchants_by_id = {m["merchant_id"]: m for m in ledger["merchants"]}
    invoices_by_debtor: dict[str, list[dict]] = {}
    for inv in ledger["invoices"]:
        invoices_by_debtor.setdefault(inv["debtor_id"], []).append(inv)

    # `is None` rather than truthiness: limit=0 means evaluate nobody, and every debtor
    # here is a live model call, so conflating it with "no limit" runs the whole book.
    debtors = ledger["debtors"] if limit is None else ledger["debtors"][:limit]
    decisions: list[StrategistDecision] = []

    as_of_date = ledger.get("as_of", "2026-08-26")
    # Folded once for the whole batch rather than re-read per debtor.
    histories = contact_history.build(date.fromisoformat(as_of_date))

    for debtor in debtors:
        d_invoices = invoices_by_debtor.get(debtor["debtor_id"], [])
        if d_invoices:
            # Fail loud, but only where the merchant actually matters. Defaulting to the
            # first merchant would silently hand the debtor whichever statutory eligibility
            # that merchant happens to have — and MER-001 is the eligible one, so a bad
            # merchant_id would quietly unlock the MSMED lever the trader merchant exists to
            # be refused. A dormant debtor with no open invoices is skipped either way, so
            # its stale reference must not abort the batch.
            merchant = merchants_by_id.get(debtor["merchant_id"])
            if merchant is None:
                audit.record(
                    "ledger.unknown_merchant",
                    debtor_id=debtor["debtor_id"],
                    merchant_id=debtor["merchant_id"],
                )
                raise KeyError(
                    f"debtor {debtor['debtor_id']} references unknown merchant "
                    f"{debtor['merchant_id']}; statutory eligibility cannot be determined"
                )
            dec = decide_for_debtor(
                debtor,
                d_invoices,
                merchant,
                as_of_date=as_of_date,
                history=histories.get(debtor["debtor_id"], NO_HISTORY),
            )
            decisions.append(dec)

    return decisions


if __name__ == "__main__":
    from app.config import PROJECT_ROOT

    parser = argparse.ArgumentParser(description="Run AI recovery strategist on ledger")
    parser.add_argument("--limit", type=int, default=10, help="Number of debtors to evaluate")
    args = parser.parse_args()

    ledger_path = PROJECT_ROOT / "data" / "ledger.json"
    if not ledger_path.exists():
        print(f"Ledger not found at {ledger_path}. Run `python -m app.ledger --seed 42 --write` first.")
        exit(1)

    ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
    print(f"Evaluating first {args.limit} debtors with AI Strategist...\n")

    results = run_strategist_batch(ledger_data, limit=args.limit)
    print(f"{'DEBTOR':<30} {'STRATEGY':<18} {'ACTION CLASS':<16} {'CONF':<6} {'REASONING'}")
    print("-" * 110)
    for r in results:
        print(f"{r.debtor_name:<30} {r.strategy:<18} {r.action_class:<16} {r.confidence:<6.2f} {r.reasoning[:40]}...")
