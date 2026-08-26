"""Checks for the decision engine, hard policy envelope, and baseline comparator.

Run with: python test_decisions.py

Verifies:
1. Hard Policy Envelope guardrails:
   - Permanent suppression on opt-out
   - Dispute blocking payment requests/escalation
   - MSMED trader statutory refusal
   - TDS underpaid allowing reconciliation
   - VIP account relationship protection
2. Baseline policy deterministic progression
3. Comparative divergence: Agent vs. Baseline across the seeded ledger
   (Milestone Gate: Agent and baseline produce different decisions across the batch)
"""

from __future__ import annotations

import json
from pathlib import Path

from app.baseline import BaselineDecision, decide_baseline, run_baseline_batch
from app.envelope import ActionClass, Strategy, evaluate_envelope
from app.ledger import InvoiceState, Merchant, UdyamActivity
from app.strategist import StrategistDecision, decide_for_debtor, run_strategist_batch


def test_envelope_opt_out() -> None:
    debtor = {
        "debtor_id": "DEB-TEST",
        "name": "Opted Out Corp",
        "opted_out": True,
        "trailing_12m_value_paise": 100_000_00,
    }
    invoices = [
        {
            "invoice_id": "INV-T1",
            "amount_paise": 500_000_00,
            "days_overdue": 30,
            "state": "OVERDUE",
        }
    ]
    merchant = Merchant("M1", "Test Merchant", True, "micro", UdyamActivity.MANUFACTURING)

    res = evaluate_envelope(debtor, invoices, merchant)
    assert res.permitted_strategies == [Strategy.WAIT], "Opt-out MUST permit only WAIT"
    assert res.action_classes[Strategy.REQUEST_PAYMENT] == ActionClass.PROHIBITED
    assert res.action_classes[Strategy.ESCALATE] == ActionClass.PROHIBITED
    assert "permanently opted out" in res.excluded_reasons[Strategy.REQUEST_PAYMENT]
    print("ok  envelope opt-out permanent suppression")


def test_envelope_disputed_invoice() -> None:
    debtor = {"debtor_id": "DEB-DISP", "name": "Disputed Ltd", "opted_out": False}
    invoices = [
        {
            "invoice_id": "INV-D1",
            "amount_paise": 200_000_00,
            "days_overdue": 10,
            "state": InvoiceState.DISPUTED,
            "dispute_reason": "Rate mismatch",
        }
    ]
    merchant = Merchant("M1", "Test Merchant", True, "micro", UdyamActivity.MANUFACTURING)

    res = evaluate_envelope(debtor, invoices, merchant)
    assert Strategy.RESOLVE_DISPUTE in res.permitted_strategies
    assert Strategy.HUMAN_HANDOFF in res.permitted_strategies
    assert Strategy.REQUEST_PAYMENT not in res.permitted_strategies
    assert Strategy.ESCALATE not in res.permitted_strategies
    assert "dispute" in res.excluded_reasons[Strategy.REQUEST_PAYMENT].lower()
    print("ok  envelope open dispute protection")


def test_envelope_msmed_trader_refusal() -> None:
    debtor = {"debtor_id": "DEB-TRD", "name": "Retail Buyer", "opted_out": False}
    invoices = [
        {
            "invoice_id": "INV-T1",
            "amount_paise": 100_000_00,
            "days_overdue": 45,
            "state": "OVERDUE",
        }
    ]
    # Trader merchant fails MSMED delayed payment provisions
    merchant = Merchant("M2", "Sagar Trading Company", True, "micro", UdyamActivity.TRADING)

    res = evaluate_envelope(debtor, invoices, merchant)
    assert not res.is_msme_eligible
    assert Strategy.ESCALATE not in res.permitted_strategies
    assert "trader" in res.excluded_reasons[Strategy.ESCALATE].lower()
    print("ok  envelope MSMED trader statutory refusal")


def test_envelope_tds_reconciliation() -> None:
    debtor = {"debtor_id": "DEB-TDS", "name": "TDS Buyer Corp", "opted_out": False}
    invoices = [
        {
            "invoice_id": "INV-TDS1",
            "amount_paise": 1_000_000_00,
            "amount_received_paise": 900_000_00,
            "tds_deducted_paise": 100_000_00,
            "days_overdue": 15,
            "state": InvoiceState.TDS_UNDERPAID,
        }
    ]
    merchant = Merchant("M1", "Test Merchant", True, "small", UdyamActivity.SERVICES)

    res = evaluate_envelope(debtor, invoices, merchant)
    assert Strategy.RECONCILE in res.permitted_strategies
    print("ok  envelope TDS reconciliation permitted")


def test_envelope_vip_protection() -> None:
    debtor = {
        "debtor_id": "DEB-VIP",
        "name": "Titan Account",
        "opted_out": False,
        "trailing_12m_value_paise": 50_000_000_00,  # 50 Lakhs TTM
    }
    invoices = [
        {
            "invoice_id": "INV-VIP1",
            "amount_paise": 50_000_00,  # 50k overdue (<1% of TTM)
            "days_overdue": 20,
            "state": "OVERDUE",
        }
    ]
    merchant = Merchant("M1", "Test Merchant", True, "small", UdyamActivity.MANUFACTURING)

    res = evaluate_envelope(debtor, invoices, merchant)
    assert Strategy.ESCALATE not in res.permitted_strategies
    assert "exposure" in res.excluded_reasons[Strategy.ESCALATE].lower()
    print("ok  envelope VIP account relationship protection")


def test_baseline_policy() -> None:
    debtor = {"debtor_id": "DEB-001", "name": "Test Debtor"}

    # Not overdue
    res_wait = decide_baseline(debtor, [{"amount_paise": 1000, "days_overdue": 0}])
    assert res_wait.strategy == Strategy.WAIT

    # Day 5 overdue
    res_r7 = decide_baseline(debtor, [{"amount_paise": 1000, "days_overdue": 5}])
    assert res_r7.strategy == Strategy.REQUEST_PAYMENT
    assert res_r7.tone == "polite"

    # Day 12 overdue
    res_r14 = decide_baseline(debtor, [{"amount_paise": 1000, "days_overdue": 12}])
    assert res_r14.strategy == Strategy.REQUEST_PAYMENT
    assert res_r14.tone == "firm"

    # Day 25 overdue
    res_r30 = decide_baseline(debtor, [{"amount_paise": 1000, "days_overdue": 25}])
    assert res_r30.strategy == Strategy.ESCALATE
    assert res_r30.tone == "urgent"

    # Day 50 overdue
    res_over = decide_baseline(debtor, [{"amount_paise": 1000, "days_overdue": 50}])
    assert res_over.strategy == Strategy.ESCALATE
    assert res_over.tone == "formal"

    print("ok  baseline policy calendar progression")


def test_batch_decision_divergence() -> None:
    """Milestone Gate: Agent and baseline both run over at least 10 debtors and produce different decisions."""
    ledger_path = Path("data/ledger.json")
    assert ledger_path.exists(), "data/ledger.json must exist"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

    # Evaluate 10 debtors
    test_debtors_count = 10
    subset_ledger = {
        **ledger,
        "debtors": ledger["debtors"][:test_debtors_count],
    }

    baseline_decisions = run_baseline_batch(subset_ledger)
    assert len(baseline_decisions) == test_debtors_count

    agent_decisions = run_strategist_batch(subset_ledger, limit=test_debtors_count)
    assert len(agent_decisions) == test_debtors_count

    # Compare baseline vs agent decisions
    divergences = 0
    reconciliation_count = 0
    dispute_count = 0
    wait_count = 0

    print("\n--- Comparative Decision Batch Run (10 Debtors) ---")
    print(f"{'Debtor Name':<28} {'Baseline Strategy':<18} {'Agent Strategy':<18} {'Divergent?':<10}")
    print("-" * 78)

    for base, agent in zip(baseline_decisions, agent_decisions):
        is_divergent = base.strategy != agent.strategy
        if is_divergent:
            divergences += 1
        if agent.strategy == Strategy.RECONCILE:
            reconciliation_count += 1
        if agent.strategy == Strategy.RESOLVE_DISPUTE:
            dispute_count += 1
        if agent.strategy == Strategy.WAIT:
            wait_count += 1

        div_marker = "YES (delta)" if is_divergent else "NO"
        print(f"{base.debtor_name:<28} {base.strategy:<18} {agent.strategy:<18} {div_marker:<10}")

    print("-" * 78)
    print(f"Total evaluated: {test_debtors_count}")
    print(f"Divergent decisions: {divergences} ({divergences/test_debtors_count:.0%})")
    print(f"Reconciliations: {reconciliation_count}, Disputes: {dispute_count}, Restraint (WAIT): {wait_count}\n")

    assert divergences >= 3, f"Agent and baseline must produce different decisions on at least 30% of debtors (got {divergences}/{test_debtors_count})"
    print("ok  decision divergence gate PASSED")


if __name__ == "__main__":
    test_envelope_opt_out()
    test_envelope_disputed_invoice()
    test_envelope_msmed_trader_refusal()
    test_envelope_tds_reconciliation()
    test_envelope_vip_protection()
    test_baseline_policy()
    test_batch_decision_divergence()
    print("\nall decision tests passed")
