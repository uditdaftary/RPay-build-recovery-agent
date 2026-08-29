"""Baseline policy runner for B2B receivables.

Non-AI reference policy representing traditional automated AR chasing.
Calendar-based escalation, zero relationship context, zero LLM calls.

Used as the counterfactual baseline in all comparative evaluations:
- Linear escalation strictly based on calendar days overdue
- Blind to TDS deductions (accuses good buyers)
- Blind to off-rail settlement (chases money already received)
- Blind to relationship value (escalates VIPs)
- Blind to opt-out (chases debtors who asked to be left alone)
- Never recommends WAIT for an overdue invoice

**Known degeneracy, and it is not a bug to tune away.** The ladder is calibrated for a
single invoice, but a decision is made per debtor, so the rung is driven by the debtor's
oldest open invoice. On `data/ledger.json` every debtor holds 2-8 invoices with ages drawn
across 1-95 days, so every debtor's oldest invoice is past 30 days and all 20 land on the
final rung. That is the correct output of this policy on this book, and it is exactly why
a bare "agent differs from baseline" count proves nothing: see `test_decisions.py`, which
asserts the collapse rather than hiding it, and compares per-case instead.
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


# (inclusive upper bound on days overdue, strategy, tone, notice label). Ordered, and read
# top down, so the first rung whose bound the invoice clears is the one that fires.
_RUNGS: tuple[tuple[int, Strategy, str, str], ...] = (
    (0, Strategy.WAIT, "neutral", "Invoice is within contractual terms; no action required."),
    (7, Strategy.REQUEST_PAYMENT, "polite", "Standard 7-day polite reminder."),
    (15, Strategy.REQUEST_PAYMENT, "firm", "Day 15 firm follow-up reminder."),
    (30, Strategy.ESCALATE, "urgent", "Day 30 overdue escalation notice."),
)
_FINAL_RUNG: tuple[Strategy, str, str] = (
    Strategy.ESCALATE,
    "formal",
    "Final statutory demand notice.",
)


def rung_for_days(days_overdue: int) -> tuple[Strategy, str, str]:
    """The calendar rung a single invoice sits on. Exposed so tests can measure the spread."""
    for bound, strategy, tone, label in _RUNGS:
        if days_overdue <= bound:
            return strategy, tone, label
    return _FINAL_RUNG


def decide_baseline(debtor: dict[str, Any], invoices: list[dict[str, Any]]) -> BaselineDecision:
    """Make a deterministic calendar-based decision for a debtor across their open invoices.

    The debtor's oldest open invoice drives the rung, which is what a per-invoice ladder
    aggregated to a per-debtor decision means. See the module docstring on why this
    collapses to a single rung on the seeded ledger.
    """
    total_amount = sum(i["amount_paise"] for i in invoices)
    total_received = sum(i.get("amount_received_paise", 0) for i in invoices)
    # Naive on purpose: the baseline does not credit TDS or off-rail settlement. That
    # blindness is the thing the comparison is meant to expose.
    naive_outstanding = total_amount - total_received

    max_days_overdue = max((i.get("days_overdue", 0) for i in invoices), default=0)
    strategy, tone, label = rung_for_days(max_days_overdue)
    overdue = max_days_overdue > 0

    return BaselineDecision(
        debtor_id=debtor.get("debtor_id", ""),
        debtor_name=debtor.get("name", ""),
        strategy=strategy,
        channel="email" if overdue else "none",
        tone=tone,
        ask_amount_paise=naive_outstanding if overdue else 0,
        days_overdue=max_days_overdue,
        reasoning=f"Invoice overdue by {max_days_overdue} days. {label}" if overdue else label,
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
