"""Checks for the decision engine, hard policy envelope, and baseline comparator.

Run with: python test_decisions.py

Everything here is deterministic and offline. The model call is stubbed where a decision
path needs exercising, so this suite costs nothing, cannot flake on a 429, and gates
honestly. The live batch against the real model chain is opt-in:

    RUN_LIVE_LLM_CHECKS=1 python test_decisions.py

Verifies:
1. Hard Policy Envelope guardrails: opt-out suppression, dispute protection, MSMED trader
   refusal, TDS reconciliation, VIP protection, unknown account value failing closed,
   settled accounts blocking money asks, and multiple exclusion grounds surviving together.
2. The agent view projection, so hidden behaviour parameters cannot reach a decision.
3. Boundary enforcement: a prohibited strategy and a below-floor concession are both
   intercepted AND leave the decision flagged for human review.
4. Baseline policy progression, and the documented collapse of that ladder on the ledger.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from app import audit, llm
from app.baseline import decide_baseline, rung_for_days, run_baseline_batch
from app.contact_history import build as build_contact_history
from app.envelope import (
    ASKS_FOR_MONEY,
    NO_CONTACT_STRATEGIES,
    NO_HISTORY,
    ActionClass,
    Channel,
    Strategy,
    evaluate_envelope,
)
from app.ledger import LEDGER_PATH, InvoiceState, Merchant, UdyamActivity, agent_view
from app.strategist import decide_for_debtor, run_strategist_batch

RUN_LIVE_LLM_CHECKS = os.getenv("RUN_LIVE_LLM_CHECKS") == "1"


@contextmanager
def isolated_audit_log():
    """Point the append-only log at a scratch directory for the duration of the suite.

    The envelope derives cooldown, escalation count and open promises from this log, so it
    is an input to a decision, not only a record of one. A suite that writes to the real
    log changes what the next run decides, and stops being repeatable — which is the exact
    property the seeded ledger exists to guarantee.
    """
    original_dir, original_log = audit.AUDIT_DIR, audit.EVENT_LOG
    with tempfile.TemporaryDirectory(prefix="recovery-agent-audit-") as scratch:
        audit.AUDIT_DIR = Path(scratch)
        audit.EVENT_LOG = audit.AUDIT_DIR / "events.jsonl"
        try:
            yield
        finally:
            audit.AUDIT_DIR, audit.EVENT_LOG = original_dir, original_log


@contextmanager
def stub_llm(payload: str):
    """Serve a fixed model response, so decision paths are checkable offline and for free."""
    original = llm.complete
    llm.complete = lambda prompt, **kwargs: payload
    try:
        yield
    finally:
        llm.complete = original


def _load_ledger() -> dict:
    assert LEDGER_PATH.exists(), f"{LEDGER_PATH} must exist; run `python -m app.ledger --seed 42 --write`"
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def _invoices_by_debtor(ledger: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for inv in ledger["invoices"]:
        grouped.setdefault(inv["debtor_id"], []).append(inv)
    return grouped


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
    # The whole set, not the two obvious members. Checking REQUEST_PAYMENT and ESCALATE
    # alone is what let OBTAIN_PROMISE and NEGOTIATE_PARTIAL survive an open dispute: both
    # are compliant ways to ask a debtor for money they have said they do not owe.
    for money_ask in sorted(ASKS_FOR_MONEY):
        assert money_ask not in res.permitted_strategies, f"{money_ask} survived an open dispute"
    assert "dispute" in res.excluded_reasons[Strategy.REQUEST_PAYMENT].lower()
    assert "dispute" in res.excluded_reasons[Strategy.OBTAIN_PROMISE].lower()
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


def _plain_debtor_and_invoice() -> tuple[dict, list[dict], Merchant]:
    debtor = {
        "debtor_id": "DEB-HIST",
        "name": "History Corp",
        "opted_out": False,
        "trailing_12m_value_paise": 10_000_00,
    }
    invoices = [
        {"invoice_id": "INV-H1", "amount_paise": 500_000_00, "days_overdue": 60, "state": "OVERDUE"}
    ]
    return debtor, invoices, Merchant("M1", "Test Merchant", True, "small", UdyamActivity.MANUFACTURING)


def _contact(debtor_id: str, ts: str, strategy: str = "REQUEST_PAYMENT", channel: str = "email") -> dict:
    return {"ts": ts, "event": "decision.made", "debtor_id": debtor_id, "strategy": strategy, "channel": channel}


def test_envelope_contact_cooldown() -> None:
    """A debtor contacted inside the cooldown is not chased again."""
    debtor, invoices, merchant = _plain_debtor_and_invoice()
    as_of = date(2026, 8, 26)

    recent = build_contact_history(as_of, [_contact("DEB-HIST", "2026-08-24T09:00:00+00:00")])
    res = evaluate_envelope(debtor, invoices, merchant, history=recent["DEB-HIST"])
    for money_ask in ASKS_FOR_MONEY:
        assert money_ask not in res.permitted_strategies, f"{money_ask} survived the cooldown"
    assert "cooldown" in res.excluded_reasons[Strategy.REQUEST_PAYMENT]
    # Responding to a state the debtor is already in stays open.
    assert Strategy.HUMAN_HANDOFF in res.permitted_strategies

    lapsed = build_contact_history(as_of, [_contact("DEB-HIST", "2026-08-15T09:00:00+00:00")])
    assert Strategy.REQUEST_PAYMENT in evaluate_envelope(
        debtor, invoices, merchant, history=lapsed["DEB-HIST"]
    ).permitted_strategies, "cooldown did not lapse after 11 days"

    # A decision not to make contact must not start a cooldown.
    silent = build_contact_history(
        as_of, [_contact("DEB-HIST", "2026-08-25T09:00:00+00:00", strategy="WAIT", channel="none")]
    )
    assert silent.get("DEB-HIST", NO_HISTORY).days_since_last_contact is None
    print("ok  envelope honours the contact-frequency cooldown")


def test_history_is_bounded_by_as_of() -> None:
    """Every fold is as at `as_of`. An event stamped later must not move any of them."""
    as_of = date(2026, 8, 26)
    later = "2026-12-01T09:00:00+00:00"

    escalations = build_contact_history(
        as_of,
        [
            _contact("D", "2026-07-01T09:00:00+00:00", strategy="ESCALATE"),
            _contact("D", later, strategy="ESCALATE"),
        ],
    )["D"]
    assert escalations.escalations_sent == 1, "an escalation after the cutoff was counted"

    future_promise = build_contact_history(
        as_of,
        [{"ts": later, "event": "promise.made", "debtor_id": "D", "promised_date": "2027-01-01"}],
    )
    assert future_promise.get("D", NO_HISTORY).active_promise_date is None, (
        "a promise recorded after the cutoff shielded the debtor before it was made"
    )

    still_open = build_contact_history(
        as_of,
        [
            {"ts": "2026-08-01T09:00:00+00:00", "event": "promise.made", "debtor_id": "D",
             "promised_date": "2026-09-15", "promised_amount_paise": 100},
            {"ts": later, "event": "settlement.confirmed", "debtor_id": "D"},
        ],
    )["D"]
    assert still_open.active_promise_date == date(2026, 9, 15), (
        "a settlement after the cutoff closed a promise that was open at it"
    )
    print("ok  contact history is bounded by as_of on every fold")


def test_history_reads_a_missing_channel_as_silence() -> None:
    """Legacy rows with no channel are suppression records, not outreach."""
    as_of = date(2026, 8, 26)
    stamp = "2026-08-25T09:00:00+00:00"

    for label, row in (
        ("absent key", {"ts": stamp, "event": "decision.made", "debtor_id": "D", "strategy": "WAIT"}),
        ("explicit null", {"ts": stamp, "event": "decision.made", "debtor_id": "D", "strategy": "WAIT", "channel": None}),
        ("channel none", _contact("D", stamp, strategy="WAIT", channel="none")),
    ):
        history = build_contact_history(as_of, [row]).get("D", NO_HISTORY)
        assert history.days_since_last_contact is None, f"{label} was counted as outreach"

    real = build_contact_history(as_of, [_contact("D", stamp)])["D"]
    assert real.days_since_last_contact == 1, "a genuine email failed to start a cooldown"
    print("ok  a decision with no channel does not start a cooldown")


def test_envelope_max_intensity() -> None:
    """Once the escalation ceiling is reached, further intensity is a human's call."""
    debtor, invoices, merchant = _plain_debtor_and_invoice()
    as_of = date(2026, 8, 26)
    events = [
        _contact("DEB-HIST", "2026-07-01T09:00:00+00:00", strategy="ESCALATE"),
        _contact("DEB-HIST", "2026-07-20T09:00:00+00:00", strategy="ESCALATE"),
    ]
    history = build_contact_history(as_of, events)["DEB-HIST"]
    assert history.escalations_sent == 2

    res = evaluate_envelope(debtor, invoices, merchant, history=history)
    assert Strategy.ESCALATE not in res.permitted_strategies
    assert "ceiling" in res.excluded_reasons[Strategy.ESCALATE]
    assert Strategy.HUMAN_HANDOFF in res.permitted_strategies, "no route left for a stuck account"
    print("ok  envelope stops at the escalation ceiling")


def test_envelope_active_promise() -> None:
    """A promise not yet due shields the debtor; a broken one does not."""
    debtor, invoices, merchant = _plain_debtor_and_invoice()
    as_of = date(2026, 8, 26)
    promise = {
        "ts": "2026-08-20T09:00:00+00:00",
        "event": "promise.made",
        "debtor_id": "DEB-HIST",
        "promised_date": "2026-09-10",
        "promised_amount_paise": 500_000_00,
    }

    open_promise = build_contact_history(as_of, [promise])["DEB-HIST"]
    res = evaluate_envelope(debtor, invoices, merchant, history=open_promise)
    for money_ask in ASKS_FOR_MONEY:
        assert money_ask not in res.permitted_strategies, f"{money_ask} chased inside an open promise"
    assert "open promise" in res.excluded_reasons[Strategy.REQUEST_PAYMENT]

    # Past its date and unsettled: broken, so chasing resumes.
    broken = build_contact_history(as_of, [{**promise, "promised_date": "2026-08-01"}])["DEB-HIST"]
    assert broken.active_promise_date is None
    assert Strategy.REQUEST_PAYMENT in evaluate_envelope(
        debtor, invoices, merchant, history=broken
    ).permitted_strategies, "a broken promise still shielded the debtor"

    # Settlement closes it, which is the webhook's suppression seen from the envelope.
    settled = build_contact_history(
        as_of,
        [promise, {"ts": "2026-08-22T09:00:00+00:00", "event": "settlement.confirmed", "debtor_id": "DEB-HIST"}],
    )
    assert settled.get("DEB-HIST", NO_HISTORY).active_promise_date is None
    print("ok  envelope holds off inside an open promise, resumes on a broken one")


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


def test_envelope_settled_account_blocks_money_asks() -> None:
    """An account settled off-rail owes nothing, so no money ask may be permitted."""
    debtor = {
        "debtor_id": "DEB-PAID",
        "name": "Already Paid Ltd",
        "opted_out": False,
        "trailing_12m_value_paise": 10_000_000_00,
    }
    invoices = [
        {
            "invoice_id": "INV-P1",
            "amount_paise": 1_000_000_00,
            "amount_received_paise": 1_000_000_00,
            "days_overdue": 60,
            "state": InvoiceState.PAID_OFF_RAIL,
            "off_rail_reference": "UTR511923447",
        }
    ]
    merchant = Merchant("M1", "Test Merchant", True, "small", UdyamActivity.MANUFACTURING)

    res = evaluate_envelope(debtor, invoices, merchant)
    assert res.collectible_paise == 0
    for money_ask in (
        Strategy.REQUEST_PAYMENT,
        Strategy.OBTAIN_PROMISE,
        Strategy.NEGOTIATE_PARTIAL,
        Strategy.ESCALATE,
    ):
        assert money_ask not in res.permitted_strategies, f"{money_ask} permitted on a settled account"
    # Chasing the UTR is still exactly right.
    assert Strategy.RECONCILE in res.permitted_strategies

    # A TDS deduction is a credit, not a shortfall: it must not inflate the collectible.
    tds_invoice = [
        {
            "invoice_id": "INV-P2",
            "amount_paise": 1_000_000_00,
            "amount_received_paise": 900_000_00,
            "tds_deducted_paise": 100_000_00,
            "days_overdue": 20,
            "state": InvoiceState.TDS_UNDERPAID,
        }
    ]
    tds_res = evaluate_envelope(debtor, tds_invoice, merchant)
    assert tds_res.collectible_paise == 0, "withheld TDS counted as collectible debt"
    print("ok  envelope blocks money asks on settled and TDS-credited accounts")


def test_envelope_unknown_account_value_fails_closed() -> None:
    """A debtor we cannot size is treated as worth protecting, not as worth nothing."""
    debtor = {"debtor_id": "DEB-UNK", "name": "Unsized Corp", "opted_out": False}
    invoices = [
        {"invoice_id": "INV-U1", "amount_paise": 40_000_00, "days_overdue": 20, "state": "OVERDUE"}
    ]
    merchant = Merchant("M1", "Test Merchant", True, "small", UdyamActivity.MANUFACTURING)

    res = evaluate_envelope(debtor, invoices, merchant)
    assert Strategy.ESCALATE not in res.permitted_strategies, "unknown account value failed open"
    assert "unknown" in res.excluded_reasons[Strategy.ESCALATE].lower()
    print("ok  envelope fails closed on unknown account value")


def test_envelope_preserves_multiple_exclusion_grounds() -> None:
    """Two rules forbidding one action must both survive into the audit trail."""
    debtor = {
        "debtor_id": "DEB-BOTH",
        "name": "Disputed And Trader",
        "opted_out": False,
        "trailing_12m_value_paise": 10_000_000_00,
    }
    invoices = [
        {
            "invoice_id": "INV-B1",
            "amount_paise": 500_000_00,
            "days_overdue": 60,
            "state": InvoiceState.DISPUTED,
            "dispute_reason": "Rate mismatch",
        }
    ]
    trader = Merchant("M2", "Sagar Trading Company", True, "micro", UdyamActivity.TRADING)

    reason = evaluate_envelope(debtor, invoices, trader).excluded_reasons[Strategy.ESCALATE]
    assert "dispute" in reason.lower(), f"dispute ground lost from: {reason}"
    assert "trader" in reason.lower(), f"statutory ground lost from: {reason}"
    print("ok  envelope preserves every independent exclusion ground")


def test_agent_view_projection() -> None:
    """Hidden behaviour parameters must not survive the projection the agent is handed."""
    raw = _load_ledger()["debtors"][0]
    assert "behaviour" in raw, "ledger row must carry hidden params or this check proves nothing"

    view = agent_view(raw)
    assert "behaviour" not in view, "hidden parameters leaked into the agent view"
    for hidden in ("pay_propensity", "promise_reliability", "habitual_days_late"):
        assert hidden not in json.dumps(view), f"{hidden} leaked into the agent view"
    assert view["debtor_id"] == raw["debtor_id"]
    print("ok  agent view projection excludes hidden behaviour parameters")


def _decision_payload(
    strategy: str,
    ask_amount_paise: int | None,
    *,
    deadline: str | None = None,
    channel: str = "email",
) -> str:
    return json.dumps(
        {
            "debtor_id": "OVERWRITTEN",
            "debtor_name": "OVERWRITTEN",
            "strategy": strategy,
            "channel": channel,
            "language": "en",
            "tone": "firm",
            "ask_amount_paise": ask_amount_paise,
            "deadline_requested": deadline,
            "reasoning": "Stubbed model response.",
            "rejected_actions": [],
            "confidence": 0.9,
            "review_required": False,
        }
    )


def _first_debtor_with_tds(ledger: dict) -> tuple[dict, list[dict], dict]:
    grouped = _invoices_by_debtor(ledger)
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}
    for debtor in ledger["debtors"]:
        invoices = grouped.get(debtor["debtor_id"], [])
        if not debtor["opted_out"] and any(
            i["state"] == InvoiceState.TDS_UNDERPAID for i in invoices
        ):
            return debtor, invoices, merchants[debtor["merchant_id"]]
    raise AssertionError("seeded ledger must contain a TDS-underpaid, non-opted-out debtor")


def _first_debtor_with_dispute(ledger: dict) -> tuple[dict, list[dict], dict]:
    grouped = _invoices_by_debtor(ledger)
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}
    for debtor in ledger["debtors"]:
        invoices = grouped.get(debtor["debtor_id"], [])
        if not debtor["opted_out"] and any(i["state"] == InvoiceState.DISPUTED for i in invoices):
            return debtor, invoices, merchants[debtor["merchant_id"]]
    raise AssertionError("seeded ledger must contain a disputed, non-opted-out debtor")


def test_intercepted_violation_requires_review() -> None:
    """A prohibited strategy is intercepted AND stays flagged for a human."""
    debtor, invoices, merchant = _first_debtor_with_dispute(_load_ledger())
    envelope = evaluate_envelope(agent_view(debtor), invoices, merchant)
    assert Strategy.REQUEST_PAYMENT not in envelope.permitted_strategies

    with stub_llm(_decision_payload("REQUEST_PAYMENT", 1)):
        decision = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)

    assert decision.strategy != Strategy.REQUEST_PAYMENT, "prohibited strategy survived"
    assert decision.review_required, "intercepted violation was not flagged for review"

    # Same for output the parser cannot read at all.
    with stub_llm("not json at all"):
        fallback = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)
    assert fallback.review_required, "parse failure was not flagged for review"
    print("ok  intercepted violations and parse failures stay flagged for review")


def test_ask_is_clamped_into_the_authorised_band() -> None:
    """Every money ask lands inside [floor, collectible], whatever the model returns."""
    ledger = _load_ledger()
    grouped = _invoices_by_debtor(ledger)
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}

    chosen: tuple[dict, list[dict], dict, object] | None = None
    for candidate in ledger["debtors"]:
        if candidate["opted_out"]:
            continue
        invoices = grouped[candidate["debtor_id"]]
        merchant = merchants[candidate["merchant_id"]]
        envelope = evaluate_envelope(agent_view(candidate), invoices, merchant)
        if Strategy.NEGOTIATE_PARTIAL in envelope.permitted_strategies:
            chosen = (candidate, invoices, merchant, envelope)
            break
    assert chosen, "seeded ledger must permit NEGOTIATE_PARTIAL for some debtor"
    debtor, invoices, merchant, envelope = chosen

    collectible = envelope.collectible_paise
    bps = int(round(envelope.max_concession_pct * 10_000))
    floor = collectible - collectible * bps // 10_000
    assert 0 < floor <= collectible

    # Below the floor, at zero, and absent entirely are all the same offence: a concession
    # the merchant never pre-authorised. Zero and None are the largest one available, so
    # they must not escape a guard that catches a 99% discount.
    for label, attempted in (("99% discount", 1_00), ("total write-off", 0), ("omitted", None)):
        with stub_llm(_decision_payload("NEGOTIATE_PARTIAL", attempted)):
            decision = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)
        assert decision.strategy == Strategy.NEGOTIATE_PARTIAL, (
            f"{label}: strategy changed, so this no longer exercises the concession path"
        )
        assert decision.ask_amount_paise == floor, f"{label} was not clamped up to the floor"
        assert decision.review_required, f"{label} was not flagged for review"

    # Above the collectible balance is the mirror offence: demanding money already paid.
    with stub_llm(_decision_payload("NEGOTIATE_PARTIAL", collectible * 10)):
        over = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)
    assert over.strategy == Strategy.NEGOTIATE_PARTIAL
    assert over.ask_amount_paise == collectible, "an over-ask was not capped at the collectible balance"
    assert over.review_required, "over-ask was not flagged for review"

    # An ask already inside the band is left exactly as the model chose it.
    with stub_llm(_decision_payload("NEGOTIATE_PARTIAL", collectible)):
        ok = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)
    assert ok.ask_amount_paise == collectible, "a compliant ask was altered"
    print("ok  money asks are clamped into the pre-authorised band and escalated")


def test_tds_withheld_cannot_be_demanded_back() -> None:
    """The naive outstanding on a TDS invoice is an over-ask, and must be capped."""
    ledger = _load_ledger()
    grouped = _invoices_by_debtor(ledger)
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}

    for debtor in ledger["debtors"]:
        invoices = grouped[debtor["debtor_id"]]
        if debtor["opted_out"] or not any(i["state"] == InvoiceState.TDS_UNDERPAID for i in invoices):
            continue
        merchant = merchants[debtor["merchant_id"]]
        envelope = evaluate_envelope(agent_view(debtor), invoices, merchant)
        naive = sum(i["amount_paise"] for i in invoices) - sum(
            i.get("amount_received_paise", 0) for i in invoices
        )
        if naive <= envelope.collectible_paise or not (ASKS_FOR_MONEY & set(envelope.permitted_strategies)):
            continue
        strategy = sorted(ASKS_FOR_MONEY & set(envelope.permitted_strategies))[0]

        with stub_llm(_decision_payload(strategy.value, naive)):
            decision = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)

        assert decision.ask_amount_paise == envelope.collectible_paise, (
            f"{debtor['debtor_id']}: agent demanded the withheld TDS back "
            f"({naive} vs collectible {envelope.collectible_paise})"
        )
        assert decision.review_required
        print("ok  withheld TDS cannot be demanded back as a shortfall")
        return

    raise AssertionError("seeded ledger must contain a TDS debtor whose naive figure overstates the debt")


def test_missing_optout_flag_is_refused() -> None:
    """A debtor row with no suppression flag must never reach the agent."""
    ledger = _load_ledger()
    raw = dict(ledger["debtors"][0])
    del raw["opted_out"]

    try:
        agent_view(raw)
    except KeyError:
        pass
    else:
        raise AssertionError("agent_view accepted a debtor with no opted_out field")

    # And the envelope itself fails closed for any caller that bypasses the projection.
    merchant = Merchant("M1", "Test Merchant", True, "small", UdyamActivity.MANUFACTURING)
    invoices = [
        {"invoice_id": "INV-M1", "amount_paise": 500_000_00, "days_overdue": 30, "state": "OVERDUE"}
    ]
    res = evaluate_envelope({"debtor_id": "DEB-NOFLAG", "name": "No Flag"}, invoices, merchant)
    assert res.permitted_strategies == [Strategy.WAIT], "absent opt-out flag failed open"
    print("ok  a debtor row with no opt-out flag is refused and fails closed")


def test_non_money_strategy_carries_no_ask() -> None:
    """RECONCILE asks for a document, not a payment, so an amount must not ride along."""
    debtor, invoices, merchant = _first_debtor_with_tds(_load_ledger())
    envelope = evaluate_envelope(agent_view(debtor), invoices, merchant)
    assert Strategy.RECONCILE in envelope.permitted_strategies
    assert Strategy.RECONCILE not in ASKS_FOR_MONEY

    with stub_llm(_decision_payload("RECONCILE", envelope.collectible_paise * 100 + 777)):
        decision = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)

    assert decision.strategy == Strategy.RECONCILE
    assert not decision.ask_amount_paise, "a non-money strategy kept an invented amount"
    assert decision.review_required, "stray ask on a non-money strategy was not flagged"
    print("ok  a non-money strategy cannot carry an ask")


def test_unusable_deadline_and_channel_are_refused() -> None:
    """A deadline already past is dropped; a channel outside the enum fails validation."""
    debtor, invoices, merchant = _first_debtor_with_tds(_load_ledger())

    past = _decision_payload("RECONCILE", None, deadline="2020-01-01")
    with stub_llm(past):
        stale = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)
    assert stale.deadline_requested is None, "a deadline in the past survived"
    assert stale.review_required, "past deadline was not flagged for review"

    # Prose where a date belongs, and a channel nobody dispatches to, must both fail
    # validation rather than reach a dispatcher — the parse-failure path catches them.
    for bad in (
        _decision_payload("RECONCILE", None, deadline="next Tuesday"),
        _decision_payload("RECONCILE", None, channel="carrier-pigeon"),
    ):
        with stub_llm(bad):
            refused = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)
        assert refused.review_required, "unvalidated delivery field was accepted"
        assert "parsing error" in refused.reasoning
    print("ok  unusable deadlines and unknown channels are refused")


def test_no_contact_strategy_carries_no_channel() -> None:
    """WAIT and HUMAN_HANDOFF reach nobody, so neither may start a contact cooldown."""
    debtor, invoices, merchant = _first_debtor_with_tds(_load_ledger())

    for strategy in sorted(NO_CONTACT_STRATEGIES):
        with stub_llm(_decision_payload(strategy.value, None, channel="email")):
            decision = decide_for_debtor(debtor, invoices, merchant, history=NO_HISTORY)
        assert decision.strategy == strategy, f"{strategy} was not the decision under test"
        assert decision.channel == Channel.NONE, f"{strategy} kept a delivery channel"
        assert decision.review_required, f"stray channel on {strategy} was not flagged"

    # The reason the channel matters: contact_history reads a channel as outreach, and the
    # envelope reads recent outreach as a reason to stop chasing. A WAIT that kept "email"
    # would suppress the account for the whole cooldown on a message never sent.
    as_of = date(2026, 8, 26)
    silenced = build_contact_history(
        as_of,
        [{"ts": "2026-08-25T10:00:00+00:00", "event": "decision.made",
          "debtor_id": "DEB-WAIT", "strategy": "WAIT", "channel": Channel.NONE}],
    )
    assert silenced.get("DEB-WAIT", NO_HISTORY).days_since_last_contact is None, (
        "a WAIT with no channel still started a cooldown"
    )
    print("ok  a strategy that contacts nobody carries no channel")


def test_decision_made_has_one_shape() -> None:
    """Both decision paths write the same `decision.made` keys, for the metrics page."""
    ledger = _load_ledger()
    grouped = _invoices_by_debtor(ledger)
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}
    opted_out = next(d for d in ledger["debtors"] if d["opted_out"])
    ordinary, ord_invoices, ord_merchant = _first_debtor_with_tds(ledger)

    seen: list[set[str]] = []
    original = audit.record

    def capture(event: str, **fields: object) -> dict:
        if event == "decision.made":
            seen.append(set(fields))
        return original(event, **fields)

    audit.record = capture
    try:
        decide_for_debtor(
            opted_out,
            grouped[opted_out["debtor_id"]],
            merchants[opted_out["merchant_id"]],
            history=NO_HISTORY,
        )
        with stub_llm(_decision_payload("RECONCILE", None)):
            decide_for_debtor(ordinary, ord_invoices, ord_merchant, history=NO_HISTORY)
    finally:
        audit.record = original

    assert len(seen) == 2, f"expected one decision.made per path, saw {len(seen)}"
    assert seen[0] == seen[1], f"suppression path differs by {seen[0] ^ seen[1]}"
    for required in ("action_class", "channel", "tone", "rejected_actions", "debtor_name"):
        assert required in seen[0], f"{required} missing from decision.made"
    print("ok  decision.made carries one shape on both paths")


def test_limit_of_zero_evaluates_nobody() -> None:
    """limit=0 means nobody. Every debtor here would otherwise be a live model call."""

    def _explode(prompt, **kwargs):
        raise AssertionError("limit=0 must not reach the model")

    original = llm.complete
    llm.complete = _explode
    try:
        assert run_strategist_batch(_load_ledger(), limit=0) == []
    finally:
        llm.complete = original
    print("ok  a limit of zero evaluates nobody")


def test_optout_suppression_needs_no_model() -> None:
    """The opt-out path must resolve to WAIT without consulting the model at all."""
    ledger = _load_ledger()
    grouped = _invoices_by_debtor(ledger)
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}
    opted_out = [d for d in ledger["debtors"] if d["opted_out"]]
    assert opted_out, "the seeded ledger must contain an opted-out debtor"

    for debtor in opted_out:
        def _explode(prompt, **kwargs):
            raise AssertionError("opted-out debtor must never reach the model")

        original = llm.complete
        llm.complete = _explode
        try:
            decision = decide_for_debtor(
                debtor,
                grouped[debtor["debtor_id"]],
                merchants[debtor["merchant_id"]],
                history=NO_HISTORY,
            )
        finally:
            llm.complete = original

        assert decision.strategy == Strategy.WAIT
        assert decision.channel == "none"
        assert "opted out" in decision.reasoning.lower()
    print("ok  opt-out suppression resolves to WAIT with no model call")


def test_baseline_ladder_and_its_collapse() -> None:
    """The ladder spans its rungs per invoice, and provably collapses per debtor."""
    ledger = _load_ledger()

    # Per invoice the ladder is genuinely graded.
    per_invoice = {rung_for_days(i["days_overdue"])[1] for i in ledger["invoices"]}
    assert len(per_invoice) >= 3, f"ladder is degenerate even per invoice: {per_invoice}"

    # Per debtor it is not, because the oldest invoice drives the rung. This is asserted
    # rather than hidden: it is the reason a bare agent-vs-baseline difference count is
    # not evidence of judgment, and it must fail loudly if the ledger ever changes shape.
    decisions = run_baseline_batch(ledger)
    rungs = {(d.strategy, d.tone) for d in decisions}
    assert rungs == {(Strategy.ESCALATE, "formal")}, (
        f"baseline no longer collapses to one rung ({rungs}); the divergence commentary in "
        "the module docstring and the timeline needs re-deriving"
    )
    assert not any(d.strategy == Strategy.WAIT for d in decisions)
    print(f"ok  baseline ladder graded per invoice, collapses to {rungs.pop()} per debtor")


def live_wait_restraint_check() -> None:
    """Opt-in. Does WAIT fire as judgment on debtors who pay late but reliably?

    The video's opening line is that the agent recommends no action on accounts that do not
    need chasing. Every WAIT observed so far came from the opt-out fast path, which is
    suppression rather than judgment, so this runs the cohort the claim is actually about.

    Selection uses agent-visible fields only. Picking the cohort by the hidden behaviour
    parameters would be choosing the answer and then reporting it as the agent's.
    """
    ledger = _load_ledger()
    grouped = _invoices_by_debtor(ledger)
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}
    as_of = ledger.get("as_of", "2026-08-26")

    ranked = []
    for debtor in ledger["debtors"]:
        if debtor["opted_out"] or not grouped.get(debtor["debtor_id"]):
            continue
        made = debtor["promises_made"]
        if made < 4 or debtor["avg_days_late"] <= 0:
            continue
        kept_ratio = debtor["promises_kept"] / made
        if kept_ratio >= 0.8:
            ranked.append((kept_ratio, debtor))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["debtor_id"]))
    cohort = [debtor for _, debtor in ranked[:5]]

    print("\n--- Live WAIT restraint check (pays late, keeps promises) ---")
    if not cohort:
        print("  no debtor in the seeded ledger matches the profile; the claim cannot be tested here")
        return
    print(f"{'Debtor':<28} {'Kept':<8} {'AvgLate':<9} {'Strategy':<18} {'Confidence'}")
    print("-" * 78)

    restraint = 0
    for debtor in cohort:
        decision = decide_for_debtor(
            debtor,
            grouped[debtor["debtor_id"]],
            merchants[debtor["merchant_id"]],
            as_of_date=as_of,
            # A fresh run: no prior contact, so nothing but the debtor's own record and the
            # invoice state can be driving the decision.
            history=NO_HISTORY,
        )
        if decision.strategy == Strategy.WAIT:
            restraint += 1
        kept = f"{debtor['promises_kept']}/{debtor['promises_made']}"
        print(
            f"{debtor['name']:<28} {kept:<8} {debtor['avg_days_late']:<9} "
            f"{decision.strategy:<18} {decision.confidence:.2f}"
        )

    print("-" * 78)
    print(f"WAIT as restraint: {restraint} of {len(cohort)}")
    if restraint == 0:
        print(
            "  NOT EVIDENCED: no debtor in this cohort drew WAIT. The opening line cannot be\n"
            "  claimed on this run, and the timeline item stays open."
        )


def live_agent_vs_baseline() -> None:
    """Opt-in. Runs the real model chain over 10 debtors and prints a per-case comparison.

    Deliberately prints rather than asserts a divergence count. Divergence measures
    difference, not correctness: against a baseline that escalates every debtor, an agent
    that always returned WAIT would score 100%. The number below is a description of one
    non-deterministic run, not a gate, and must not be quoted as a result without saying
    which models served it.
    """
    ledger = _load_ledger()
    count = 10
    subset = {**ledger, "debtors": ledger["debtors"][:count]}

    baseline_decisions = run_baseline_batch(subset)
    agent_decisions = run_strategist_batch(subset, limit=count)

    # Paired on debtor_id, not by position. The two runners live in different modules and
    # filter independently, so positional pairing would silently attribute one debtor's
    # decision to another company's baseline the moment either filter changed.
    baseline_by_id = {d.debtor_id: d for d in baseline_decisions}
    agent_ids = {d.debtor_id for d in agent_decisions}
    assert agent_ids == set(baseline_by_id), (
        "agent and baseline batches covered different debtors "
        f"(agent-only {sorted(agent_ids - set(baseline_by_id))}, "
        f"baseline-only {sorted(set(baseline_by_id) - agent_ids)}); the comparison would "
        "attribute decisions to the wrong companies"
    )

    print("\n--- Live agent vs baseline (non-deterministic, descriptive only) ---")
    print(f"{'Debtor':<28} {'Baseline':<18} {'Agent':<18} {'Differs':<8} {'Review'}")
    print("-" * 86)
    for agent in agent_decisions:
        base = baseline_by_id[agent.debtor_id]
        differs = "yes" if base.strategy != agent.strategy else "no"
        print(
            f"{base.debtor_name:<28} {base.strategy:<18} {agent.strategy:<18} "
            f"{differs:<8} {agent.review_required}"
        )

    opted_out_ids = {d["debtor_id"] for d in subset["debtors"] if d["opted_out"]}
    restraint = [
        d for d in agent_decisions if d.strategy == Strategy.WAIT and d.debtor_id not in opted_out_ids
    ]
    print(
        f"\nWAIT decisions that are genuine restraint rather than opt-out suppression: "
        f"{len(restraint)}"
    )
    if not restraint:
        print(
            "  NOTE: every WAIT this run came from the opt-out fast path. The claim that "
            "restraint fires on reliable late payers is NOT evidenced by this run."
        )


if __name__ == "__main__":
    with isolated_audit_log():
        test_envelope_opt_out()
        test_envelope_disputed_invoice()
        test_envelope_msmed_trader_refusal()
        test_envelope_tds_reconciliation()
        test_envelope_vip_protection()
        test_envelope_settled_account_blocks_money_asks()
        test_envelope_unknown_account_value_fails_closed()
        test_envelope_preserves_multiple_exclusion_grounds()
        test_agent_view_projection()
        test_missing_optout_flag_is_refused()
        test_intercepted_violation_requires_review()
        test_ask_is_clamped_into_the_authorised_band()
        test_tds_withheld_cannot_be_demanded_back()
        test_non_money_strategy_carries_no_ask()
        test_no_contact_strategy_carries_no_channel()
        test_unusable_deadline_and_channel_are_refused()
        test_decision_made_has_one_shape()
        test_limit_of_zero_evaluates_nobody()
        test_optout_suppression_needs_no_model()
        test_envelope_contact_cooldown()
        test_history_is_bounded_by_as_of()
        test_history_reads_a_missing_channel_as_silence()
        test_envelope_max_intensity()
        test_envelope_active_promise()
        test_baseline_policy()
        test_baseline_ladder_and_its_collapse()
        print("\nall decision tests passed")

        if RUN_LIVE_LLM_CHECKS:
            live_wait_restraint_check()
            live_agent_vs_baseline()
        else:
            print("\nskipped the live model batch; set RUN_LIVE_LLM_CHECKS=1 to run it")
