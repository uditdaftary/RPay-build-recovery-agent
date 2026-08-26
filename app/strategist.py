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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app import audit, llm
from app.envelope import ActionClass, EnvelopeResult, Strategy, evaluate_envelope


class RejectedAction(BaseModel):
    strategy: Strategy
    reason: str


class StrategistDecision(BaseModel):
    debtor_id: str
    debtor_name: str
    strategy: Strategy
    channel: str  # email | whatsapp | portal | none
    language: str  # en | hi | hinglish
    tone: str  # collaborative | firm | formal | conciliatory | neutral
    ask_amount_paise: int | None = None
    deadline_requested: str | None = None
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
"""


def _format_inr(paise: int) -> str:
    rupees = paise // 100
    return f"Rs {rupees:,}"


def decide_for_debtor(
    debtor: dict[str, Any],
    invoices: list[dict[str, Any]],
    merchant: dict[str, Any],
    *,
    as_of_date: str = "2026-08-26",
) -> StrategistDecision:
    """Evaluate envelope and call the LLM to generate a recovery decision for a debtor."""
    envelope: EnvelopeResult = evaluate_envelope(debtor, invoices, merchant)

    debtor_id = debtor.get("debtor_id", "")
    debtor_name = debtor.get("name", "")

    # Fast-path / hard-suppression: if only WAIT is permitted (e.g. permanent opt-out)
    if envelope.permitted_strategies == [Strategy.WAIT]:
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
        audit.record(
            "decision.made",
            debtor_id=debtor_id,
            strategy=decision.strategy,
            review_required=decision.review_required,
            reasoning=decision.reasoning,
        )
        return decision

    # Build prompt payload
    total_amount_paise = sum(i["amount_paise"] for i in invoices)
    total_received_paise = sum(i.get("amount_received_paise", 0) for i in invoices)
    total_overdue_paise = total_amount_paise - total_received_paise

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
        "total_outstanding": _format_inr(total_overdue_paise),
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

    envelope_summary = {
        "permitted_strategies": [s.value for s in envelope.permitted_strategies],
        "excluded_strategies": {s.value: r for s, r in envelope.excluded_reasons.items()},
        "max_concession_pct": envelope.max_concession_pct,
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
        decision = StrategistDecision(
            debtor_id=debtor_id,
            debtor_name=debtor_name,
            strategy=fallback_strategy,
            channel="none",
            language=debtor.get("language", "en"),
            tone="neutral",
            ask_amount_paise=total_overdue_paise,
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

    # Set action class and review_required flag from envelope
    decision.action_class = envelope.action_classes.get(decision.strategy, ActionClass.AUTOMATABLE)
    decision.review_required = decision.action_class == ActionClass.REVIEW_REQUIRED

    audit.record(
        "decision.made",
        debtor_id=debtor_id,
        debtor_name=debtor_name,
        strategy=decision.strategy,
        action_class=decision.action_class,
        review_required=decision.review_required,
        channel=decision.channel,
        tone=decision.tone,
        reasoning=decision.reasoning,
        rejected_actions=[r.model_dump() for r in decision.rejected_actions],
    )

    return decision


def run_strategist_batch(
    ledger: dict[str, Any], *, limit: int | None = None
) -> list[StrategistDecision]:
    """Run AI recovery strategist over debtors in the ledger."""
    merchants_by_id = {m["merchant_id"]: m for m in ledger["merchants"]}
    invoices_by_debtor: dict[str, list[dict]] = {}
    for inv in ledger["invoices"]:
        invoices_by_debtor.setdefault(inv["debtor_id"], []).append(inv)

    debtors = ledger["debtors"][:limit] if limit else ledger["debtors"]
    decisions: list[StrategistDecision] = []

    for debtor in debtors:
        d_invoices = invoices_by_debtor.get(debtor["debtor_id"], [])
        merchant = merchants_by_id.get(debtor["merchant_id"], ledger["merchants"][0])
        if d_invoices:
            dec = decide_for_debtor(debtor, d_invoices, merchant, as_of_date=ledger.get("as_of", "2026-08-26"))
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
