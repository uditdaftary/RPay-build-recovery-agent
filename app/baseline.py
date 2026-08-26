"""Baseline policy runner for B2B receivables.

Non-AI reference policy representing traditional automated AR chasing.
Fixed cadence, calendar-based escalation, zero relationship context, zero LLM calls.

Used as the counterfactual baseline in all comparative evaluations:
- Email every 7 days
- Linear escalation strictly based on calendar days overdue
- Blind to TDS deductions (accuses good buyers)
- Blind to relationship value (escalates VIPs)
- Never recommends WAIT for an overdue invoice
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.envelope import Strategy


@dataclass(frozen=True)
class BaselineDecision:
    debtor_id: str
    debtor_name: str
    strategy: Strategy
    channel: str
    tone: str
    ask_amount_paise: int
    days_overdue: int
    reasoning: str


def decide_baseline(debtor: dict[str, Any], invoices: list[dict[str, Any]]) -> BaselineDecision:
    """Make a deterministic calendar-based decision for a debtor across their open invoices."""
    debtor_id = debtor.get("debtor_id", "")
    debtor_name = debtor.get("name", "")

    total_amount = sum(i["amount_paise"] for i in invoices)
    total_received = sum(i.get("amount_received_paise", 0) for i in invoices)
    naive_outstanding = total_amount - total_received

    max_days_overdue = max((i.get("days_overdue", 0) for i in invoices), default=0)

    if max_days_overdue <= 0:
        return BaselineDecision(
            debtor_id=debtor_id,
            debtor_name=debtor_name,
            strategy=Strategy.WAIT,
            channel="none",
            tone="neutral",
            ask_amount_paise=0,
            days_overdue=max_days_overdue,
            reasoning="Invoice is within contractual terms; no action required.",
        )
    elif max_days_overdue <= 7:
        return BaselineDecision(
            debtor_id=debtor_id,
            debtor_name=debtor_name,
            strategy=Strategy.REQUEST_PAYMENT,
            channel="email",
            tone="polite",
            ask_amount_paise=naive_outstanding,
            days_overdue=max_days_overdue,
            reasoning=f"Invoice overdue by {max_days_overdue} days. Standard 7-day polite reminder.",
        )
    elif max_days_overdue <= 15:
        return BaselineDecision(
            debtor_id=debtor_id,
            debtor_name=debtor_name,
            strategy=Strategy.REQUEST_PAYMENT,
            channel="email",
            tone="firm",
            ask_amount_paise=naive_outstanding,
            days_overdue=max_days_overdue,
            reasoning=f"Invoice overdue by {max_days_overdue} days. Day 14 firm follow-up reminder.",
        )
    elif max_days_overdue <= 30:
        return BaselineDecision(
            debtor_id=debtor_id,
            debtor_name=debtor_name,
            strategy=Strategy.ESCALATE,
            channel="email",
            tone="urgent",
            ask_amount_paise=naive_outstanding,
            days_overdue=max_days_overdue,
            reasoning=f"Invoice overdue by {max_days_overdue} days. Day 30 overdue escalation notice.",
        )
    else:
        return BaselineDecision(
            debtor_id=debtor_id,
            debtor_name=debtor_name,
            strategy=Strategy.ESCALATE,
            channel="email",
            tone="formal",
            ask_amount_paise=naive_outstanding,
            days_overdue=max_days_overdue,
            reasoning=f"Invoice overdue by {max_days_overdue} days (>30d). Final statutory demand notice.",
        )


def run_baseline_batch(ledger: dict[str, Any]) -> list[BaselineDecision]:
    """Run baseline policy over all debtors in a ledger."""
    invoices_by_debtor: dict[str, list[dict]] = {}
    for inv in ledger["invoices"]:
        invoices_by_debtor.setdefault(inv["debtor_id"], []).append(inv)

    decisions: list[BaselineDecision] = []
    for debtor in ledger["debtors"]:
        d_invoices = invoices_by_debtor.get(debtor["debtor_id"], [])
        if d_invoices:
            decisions.append(decide_baseline(debtor, d_invoices))

    return decisions
