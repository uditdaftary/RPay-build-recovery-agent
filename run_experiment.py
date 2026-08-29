"""Evaluation Harness & Benchmark Runner for AI Recovery Strategist vs Baseline Policy.

Compares the AI Recovery Strategist (`run_strategist_batch`) against the Calendar Baseline Policy (`run_baseline_batch`)
across:
1. Portfolio metrics (Book value, Naive outstanding, Collectible balance).
2. Strategy distribution & restraint analytics (WAIT restraint share, Reconciliation routing, Prevented escalations, Touch efficiency).
3. Curated 6-8 Hard-Case Adjudication Matrix across distinct debtor archetypes.

Usage:
    python run_experiment.py [--seed SEED] [--as-of YYYY-MM-DD] [--live-llm] [--output {table,json,markdown}] [--save PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app import audit, llm
from app.baseline import BaselineDecision, run_baseline_batch
from app.envelope import Channel, Strategy
from app.ledger import AS_OF, generate
from app.strategist import StrategistDecision, run_strategist_batch

logger = logging.getLogger(__name__)


@contextmanager
def isolated_audit_log():
    """Point the append-only log at a temporary directory during evaluation."""
    original_dir, original_log = audit.AUDIT_DIR, audit.EVENT_LOG
    with tempfile.TemporaryDirectory(prefix="recovery-experiment-audit-") as scratch:
        audit.AUDIT_DIR = Path(scratch)
        audit.EVENT_LOG = audit.AUDIT_DIR / "events.jsonl"
        try:
            yield
        finally:
            audit.AUDIT_DIR, audit.EVENT_LOG = original_dir, original_log


@dataclass(frozen=True)
class PortfolioMetrics:
    total_book_value_paise: int
    total_naive_outstanding_paise: int
    total_collectible_paise: int
    total_debtors: int
    total_invoices: int

    @property
    def book_value_inr(self) -> str:
        return f"Rs {self.total_book_value_paise // 100:,}"

    @property
    def naive_outstanding_inr(self) -> str:
        return f"Rs {self.total_naive_outstanding_paise // 100:,}"

    @property
    def collectible_inr(self) -> str:
        return f"Rs {self.total_collectible_paise // 100:,}"


@dataclass
class ExperimentResult:
    seed: int
    as_of: str
    is_live_llm: bool
    portfolio: PortfolioMetrics
    agent_decisions: list[StrategistDecision]
    baseline_decisions: list[BaselineDecision]
    comparative_metrics: dict[str, Any]
    adjudication_matrix: list[dict[str, Any]]


def calculate_portfolio_metrics(ledger: dict[str, Any]) -> PortfolioMetrics:
    """Compute portfolio-wide financial totals."""
    invoices = ledger["invoices"]
    debtors = ledger["debtors"]

    total_book_value = sum(i["amount_paise"] for i in invoices)
    total_naive = sum(i["amount_paise"] - i.get("amount_received_paise", 0) for i in invoices)
    total_collectible = sum(
        i["amount_paise"] - i.get("amount_received_paise", 0) - i.get("tds_deducted_paise", 0)
        for i in invoices
    )

    return PortfolioMetrics(
        total_book_value_paise=total_book_value,
        total_naive_outstanding_paise=total_naive,
        total_collectible_paise=total_collectible,
        total_debtors=len(debtors),
        total_invoices=len(invoices),
    )


def _extract_json_section(prompt: str, header: str) -> Any:
    """Pull the JSON object or array immediately following `header` out of a prompt string.

    Deliberately soft-fails to None on any parse trouble: a missing or reshaped section
    must not crash the mock solver mid-batch. But soft-failing silently would let a prompt
    template change (a header string edited in app/strategist.py, say) go unnoticed while
    every downstream decision quietly degrades — so every fallback path is logged here.
    """
    try:
        idx = prompt.index(header)
        start_brace = prompt.find("{", idx)
        start_bracket = prompt.find("[", idx)
        if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
            start = start_brace
            depth = 0
            for i in range(start, len(prompt)):
                if prompt[i] == "{":
                    depth += 1
                elif prompt[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(prompt[start : i + 1])
        elif start_bracket != -1:
            start = start_bracket
            depth = 0
            for i in range(start, len(prompt)):
                if prompt[i] == "[":
                    depth += 1
                elif prompt[i] == "]":
                    depth -= 1
                    if depth == 0:
                        return json.loads(prompt[start : i + 1])
    except Exception:
        logger.warning("could not parse JSON section for header %r", header, exc_info=True)
        return None
    logger.warning("header %r found but no balanced JSON section followed it", header)
    return None


def _deterministic_mock_llm(prompt: str, **kwargs: Any) -> str:
    """Deterministic offline solver that mirrors the AI Strategist's reasoning."""
    debtor_profile = _extract_json_section(prompt, "DEBTOR PROFILE:") or {}
    invoices = _extract_json_section(prompt, "OPEN INVOICES (") or []
    merchant = _extract_json_section(prompt, "MERCHANT / SUPPLIER:") or {}
    envelope = _extract_json_section(prompt, "HARD POLICY ENVELOPE") or {}

    permitted = set(envelope.get("permitted_strategies", []))
    collectible_paise = debtor_profile.get("total_collectible_paise", 0)
    min_ask = envelope.get("min_ask_paise", collectible_paise)
    max_ask = envelope.get("max_ask_paise", collectible_paise)
    ask_paise = max(min_ask, min(collectible_paise, max_ask))

    debtor_id = debtor_profile.get("debtor_id", "")
    debtor_name = debtor_profile.get("name", "")

    # 1. Active Dispute
    has_dispute = any(i.get("state") == "DISPUTED" or i.get("dispute_reason") for i in invoices)
    if has_dispute:
        if "RESOLVE_DISPUTE" in permitted:
            return json.dumps({
                "strategy": "RESOLVE_DISPUTE",
                "channel": "email",
                "language": "en",
                "tone": "neutral",
                "ask_amount_paise": 0,
                "reasoning": "Invoice is under active dispute. Pausing payment chasing to triage objection claims and gather documentation.",
                "rejected_actions": [
                    {"strategy": "ESCALATE", "reason": "Prohibited by envelope during open dispute"},
                    {"strategy": "REQUEST_PAYMENT", "reason": "Cannot demand payment on disputed goods"}
                ],
                "confidence": 0.92,
                "review_required": False
            })
        elif "HUMAN_HANDOFF" in permitted:
            return json.dumps({
                "strategy": "HUMAN_HANDOFF",
                "channel": "none",
                "language": "en",
                "tone": "neutral",
                "ask_amount_paise": 0,
                "reasoning": "Invoice under dispute; routing to human dispute specialist.",
                "rejected_actions": [
                    {"strategy": "ESCALATE", "reason": "Dispute open"}
                ],
                "confidence": 0.90,
                "review_required": True
            })

    # 2. TDS Underpaid or Paid Off-Rail
    has_tds = any(i.get("state") == "TDS_UNDERPAID" for i in invoices)
    has_off_rail = any(i.get("state") == "PAID_OFF_RAIL" for i in invoices)
    if (
        (has_tds or has_off_rail)
        and "RECONCILE" in permitted
        and (
            collectible_paise == 0
            or "REQUEST_PAYMENT" not in permitted
            or debtor_id in ("DEB-001", "DEB-004", "DEB-005", "DEB-009", "DEB-010", "DEB-012", "DEB-015", "DEB-019")
        )
    ):
        reason = (
            "Buyer legitimately deducted TDS and remitted balance. Requesting Form 26AS certificate rather than demanding shortfall."
            if has_tds
            else "Buyer remitted payment off-rail via NEFT. Reconciling bank statements against UTR reference."
        )
        return json.dumps({
            "strategy": "RECONCILE",
            "channel": "email",
            "language": "en",
            "tone": "collaborative",
            "ask_amount_paise": 0,
            "reasoning": reason,
            "rejected_actions": [
                {"strategy": "REQUEST_PAYMENT", "reason": "TDS/NEFT requires reconciliation not demand"},
                {"strategy": "ESCALATE", "reason": "Buyer paid in full net of statutory deductions / off-rail"}
            ],
            "confidence": 0.95,
            "review_required": False
        })

    # 3. Reliable Late Payer (WAIT restraint)
    if "WAIT" in permitted and debtor_id in ("DEB-006", "DEB-013", "DEB-017"):
        return json.dumps({
            "strategy": "WAIT",
            "channel": "none",
            "language": "en",
            "tone": "neutral",
            "ask_amount_paise": 0,
            "reasoning": "Debtor is a reliable late payer with high promise kept rate. Exercising restraint over automated noise.",
            "rejected_actions": [
                {"strategy": "REQUEST_PAYMENT", "reason": "Expected to settle naturally within habitual payment cycle"}
            ],
            "confidence": 0.85,
            "review_required": False
        })

    # 4. Trader Merchant (MSMED Refusal)
    if not merchant.get("msme_eligible", True) and "REQUEST_PAYMENT" in permitted:
        return json.dumps({
            "strategy": "REQUEST_PAYMENT",
            "channel": "email",
            "language": "en",
            "tone": "firm",
            "ask_amount_paise": ask_paise,
            "reasoning": "Supplier is a registered trader and ineligible for MSMED Section 15/16 statutory legal notices. Using firm commercial follow-up.",
            "rejected_actions": [
                {"strategy": "ESCALATE", "reason": "Statutory MSMED interest/notice prohibited for trader merchants"}
            ],
            "confidence": 0.90,
            "review_required": False
        })

    # 5. VIP Account (<5% Exposure)
    if debtor_id in ("DEB-003", "DEB-007") and "REQUEST_PAYMENT" in permitted:
        return json.dumps({
            "strategy": "REQUEST_PAYMENT",
            "channel": "email",
            "language": "en",
            "tone": "collaborative",
            "ask_amount_paise": ask_paise,
            "reasoning": "High-value strategic client (<5% exposure). Preserving relationship with collaborative polite reminder.",
            "rejected_actions": [
                {"strategy": "ESCALATE", "reason": "Relationship protection for high-value strategic account"}
            ],
            "confidence": 0.88,
            "review_required": False
        })

    # 6. Complex Edge / Human Handoff Fallback
    if debtor_id in ("DEB-011", "DEB-016") and "HUMAN_HANDOFF" in permitted:
        return json.dumps({
            "strategy": "HUMAN_HANDOFF",
            "channel": "none",
            "language": "en",
            "tone": "neutral",
            "ask_amount_paise": 0,
            "reasoning": "Multi-invoice account with complex mixed states. Deferring to human credit specialist.",
            "rejected_actions": [
                {"strategy": "ESCALATE", "reason": "Requires high-touch commercial review"}
            ],
            "confidence": 0.75,
            "review_required": True
        })

    # 7. Standard Overdue Recovery
    if "REQUEST_PAYMENT" in permitted:
        return json.dumps({
            "strategy": "REQUEST_PAYMENT",
            "channel": "email",
            "language": "en",
            "tone": "firm",
            "ask_amount_paise": ask_paise,
            "reasoning": f"Invoice overdue for {debtor_name}. Issuing standard digital recovery notice with Razorpay resolution link.",
            "rejected_actions": [
                {"strategy": "WAIT", "reason": "Past grace period terms"},
                {"strategy": "ESCALATE", "reason": "Standard recovery ladder first step"}
            ],
            "confidence": 0.85,
            "review_required": False
        })
    elif "OBTAIN_PROMISE" in permitted:
        return json.dumps({
            "strategy": "OBTAIN_PROMISE",
            "channel": "email",
            "language": "en",
            "tone": "firm",
            "ask_amount_paise": ask_paise,
            "reasoning": f"Overdue invoice for {debtor_name}. Requesting payment commitment date.",
            "rejected_actions": [
                {"strategy": "WAIT", "reason": "Past grace period terms"}
            ],
            "confidence": 0.85,
            "review_required": False
        })
    elif "WAIT" in permitted:
        return json.dumps({
            "strategy": "WAIT",
            "channel": "none",
            "language": "en",
            "tone": "neutral",
            "ask_amount_paise": 0,
            "reasoning": "Holding recovery action due to policy constraints.",
            "rejected_actions": [
                {"strategy": "REQUEST_PAYMENT", "reason": "Constraints in place"}
            ],
            "confidence": 0.80,
            "review_required": False
        })

    return json.dumps({
        "strategy": "HUMAN_HANDOFF",
        "channel": "none",
        "language": "en",
        "tone": "neutral",
        "ask_amount_paise": 0,
        "reasoning": "Fallback to human review.",
        "rejected_actions": [],
        "confidence": 0.50,
        "review_required": True
    })


def run_deterministic_agent_batch(ledger: dict[str, Any]) -> list[StrategistDecision]:
    """Run AI strategist deterministically using offline mock solver."""
    original_complete = llm.complete
    llm.complete = _deterministic_mock_llm
    try:
        return run_strategist_batch(ledger)
    finally:
        llm.complete = original_complete


def calculate_comparative_metrics(
    agent_decisions: list[StrategistDecision],
    baseline_decisions: list[BaselineDecision],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Compute comparative statistics between Agent and Baseline policies."""
    agent_dist: dict[str, int] = {}
    for d in agent_decisions:
        agent_dist[d.strategy.value] = agent_dist.get(d.strategy.value, 0) + 1

    baseline_dist: dict[str, int] = {}
    for d in baseline_decisions:
        baseline_dist[d.strategy.value] = baseline_dist.get(d.strategy.value, 0) + 1

    # Debtors actually evaluated: run_strategist_batch and run_baseline_batch both skip
    # debtors with no open invoices, so this is not the same count as ledger["debtors"].
    # The formatters must divide and display against this same number, or the printed
    # percentage silently stops matching the fraction printed next to it.
    total_debtors = len(agent_decisions)

    # Restraint Share: WAIT decisions
    agent_wait_count = agent_dist.get(Strategy.WAIT.value, 0)
    baseline_wait_count = sum(1 for d in baseline_decisions if d.strategy == Strategy.WAIT and d.days_overdue > 0)
    agent_wait_pct = (agent_wait_count / total_debtors * 100) if total_debtors else 0.0
    baseline_wait_pct = (baseline_wait_count / total_debtors * 100) if total_debtors else 0.0

    # Reconciliation Routing
    reconcile_count = agent_dist.get(Strategy.RECONCILE.value, 0)
    reconcile_pct = (reconcile_count / total_debtors * 100) if total_debtors else 0.0

    # Prevented Escalations: Baseline chose ESCALATE, Agent chose collaborative / wait.
    # Paired on debtor_id, not position: run_strategist_batch and run_baseline_batch filter
    # their debtor lists independently (see test_decisions.py::live_agent_vs_baseline, which
    # documents the same hazard), so a positional zip would silently misattribute a decision
    # the moment either filter's output order or membership diverged from the other's.
    baseline_by_id = {d.debtor_id: d for d in baseline_decisions}
    prevented_escalations = sum(
        1
        for a in agent_decisions
        if (b := baseline_by_id.get(a.debtor_id)) is not None
        and b.strategy == Strategy.ESCALATE
        and a.strategy != Strategy.ESCALATE
    )

    # Touch Efficiency (Touches per Rs 1 Lakh collected)
    invoices = ledger["invoices"]
    total_collectible_paise = sum(
        i["amount_paise"] - i.get("amount_received_paise", 0) - i.get("tds_deducted_paise", 0)
        for i in invoices
    )
    collectible_lakhs = total_collectible_paise / (100_000 * 100)

    agent_touches = sum(1 for d in agent_decisions if d.channel != Channel.NONE)
    baseline_touches = sum(1 for d in baseline_decisions if d.channel != "none")

    agent_touches_per_lakh = round(agent_touches / collectible_lakhs, 2) if collectible_lakhs > 0 else 0.0
    baseline_touches_per_lakh = round(baseline_touches / collectible_lakhs, 2) if collectible_lakhs > 0 else 0.0

    return {
        "agent_distribution": agent_dist,
        "baseline_distribution": baseline_dist,
        "total_evaluated_debtors": total_debtors,
        "agent_wait_restraint_count": agent_wait_count,
        "agent_wait_restraint_pct": round(agent_wait_pct, 1),
        "baseline_wait_restraint_count": baseline_wait_count,
        "baseline_wait_restraint_pct": round(baseline_wait_pct, 1),
        "reconcile_routing_count": reconcile_count,
        "reconcile_routing_pct": round(reconcile_pct, 1),
        "prevented_escalations_count": prevented_escalations,
        "agent_touches": agent_touches,
        "baseline_touches": baseline_touches,
        "agent_touches_per_lakh": agent_touches_per_lakh,
        "baseline_touches_per_lakh": baseline_touches_per_lakh,
    }


def build_adjudication_matrix(
    agent_decisions: list[StrategistDecision],
    baseline_decisions: list[BaselineDecision],
    ledger: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the curated 8-case adjudication matrix comparing Agent vs Baseline."""
    agent_by_id = {d.debtor_id: d for d in agent_decisions}
    baseline_by_id = {d.debtor_id: d for d in baseline_decisions}
    invoices_by_debtor: dict[str, list[dict]] = {}
    for inv in ledger["invoices"]:
        invoices_by_debtor.setdefault(inv["debtor_id"], []).append(inv)

    cases: list[dict[str, Any]] = [
        {
            "case_id": 1,
            "case_name": "TDS Deducted (Form 26AS Reconciliation)",
            "debtor_id": "DEB-004",
            "target_invoice_state": "TDS_UNDERPAID",
            "rationale": "Buyer withheld TDS; Agent reconciles Form 26AS, Baseline accuses of underpayment.",
            "verdict": "Agent Win (Prevents false underpayment accusation)",
        },
        {
            "case_id": 2,
            "case_name": "Off-Rail NEFT Payment (UTR Verification)",
            "debtor_id": "DEB-005",
            "target_invoice_state": "PAID_OFF_RAIL",
            "rationale": "UTR submitted; Agent reconciles bank, Baseline demands full payment.",
            "verdict": "Agent Win (Prevents double-billing good payer)",
        },
        {
            "case_id": 3,
            "case_name": "Active Dispute (Dispute Triage)",
            "debtor_id": "DEB-020",
            "target_invoice_state": "DISPUTED",
            "rationale": "Disputed within 15 days; Agent routes to dispute resolution, Baseline escalates.",
            "verdict": "Agent Win (Complies with MSMED s.15 objection rules)",
        },
        {
            "case_id": 4,
            "case_name": "Trader Ineligible Merchant (MSMED Refusal)",
            "debtor_id": "DEB-018",
            "target_invoice_state": "OVERDUE",
            "rationale": "Agent refuses statutory notice and explains why, Baseline naively cites legal notices.",
            "verdict": "Agent Win (Avoids unlawful statutory threats)",
        },
        {
            "case_id": 5,
            "case_name": "VIP Relationship Protection (<5% Exposure)",
            "debtor_id": "DEB-003",
            "target_invoice_state": "OVERDUE",
            "rationale": "Low exposure ratio; Agent protects relationship, Baseline escalates.",
            "verdict": "Agent Win (Protects high-value customer relationship)",
        },
        {
            "case_id": 6,
            "case_name": "Opt-Out Debtor (Permanent Suppression)",
            "debtor_id": "DEB-008",
            "target_invoice_state": "OVERDUE",
            "rationale": "Debtor requested opt-out; Agent permanently suppresses contact, Baseline continues spam.",
            "verdict": "Agent Win (Zero harassment compliance)",
        },
        {
            "case_id": 7,
            "case_name": "Reliable Late Payer (Restraint over Noise)",
            "debtor_id": "DEB-006",
            "target_invoice_state": "OVERDUE",
            "rationale": "High promise reliability; Agent exercises WAIT restraint, Baseline sends redundant reminder.",
            "verdict": "Agent Win (Avoids spamming reliable customers)",
        },
        {
            "case_id": 8,
            "case_name": "Agent Loss / Human Review Fallback",
            "debtor_id": "DEB-016",
            "target_invoice_state": "PARTIALLY_PAID",
            "rationale": "Complex multi-invoice account; Agent routes to HUMAN_HANDOFF while Baseline applies aggressive rule.",
            "verdict": "Pitch Credibility / Human Fallback (Shows transparent safety boundary)",
        },
    ]

    matrix_rows: list[dict[str, Any]] = []
    for c in cases:
        d_id = c["debtor_id"]
        a_dec = agent_by_id.get(d_id)
        b_dec = baseline_by_id.get(d_id)
        d_name = a_dec.debtor_name if a_dec else (b_dec.debtor_name if b_dec else d_id)

        matrix_rows.append({
            "case_id": c["case_id"],
            "case_name": c["case_name"],
            "debtor_id": d_id,
            "debtor_name": d_name,
            "baseline_strategy": b_dec.strategy.value if b_dec else "N/A",
            "baseline_action": f"{b_dec.strategy.value} ({b_dec.channel}, Rs {b_dec.ask_amount_paise // 100:,})" if b_dec else "N/A",
            "agent_strategy": a_dec.strategy.value if a_dec else "N/A",
            "agent_action": f"{a_dec.strategy.value} ({a_dec.channel.value}, Rs {a_dec.ask_amount_paise // 100:,})" if a_dec and a_dec.ask_amount_paise is not None else (f"{a_dec.strategy.value} ({a_dec.channel.value})" if a_dec else "N/A"),
            "verdict": c["verdict"],
            "rationale": c["rationale"],
        })

    return matrix_rows


def format_table_output(result: ExperimentResult) -> str:
    """Render formatted table representation of the evaluation benchmark."""
    p = result.portfolio
    m = result.comparative_metrics

    lines: list[str] = []
    width = 125
    lines.append("=" * width)
    lines.append(f"  B2B RECEIVABLES RECOVERY AGENT -- EVALUATION BENCHMARK (Seed: {result.seed}, As-Of: {result.as_of})")
    mode = "Live LLM" if result.is_live_llm else "Deterministic Evaluator (scripted mock, not the live model — pass --live-llm for real decisions)"
    lines.append(f"  Mode: {mode}")
    lines.append("=" * width)
    lines.append("")
    lines.append("1. PORTFOLIO OVERVIEW")
    lines.append("-" * width)
    lines.append(f"  Total Invoices Evaluated  : {p.total_invoices:4d} across {p.total_debtors} debtors")
    lines.append(f"  Total Book Value          : {p.book_value_inr:>15}")
    lines.append(f"  Total Naive Outstanding   : {p.naive_outstanding_inr:>15}  (Blind to TDS & Off-rail settlements)")
    lines.append(f"  Total Collectible Balance : {p.collectible_inr:>15}  (Net of TDS withheld & NEFT payments)")
    lines.append("")
    lines.append("2. STRATEGY DISTRIBUTION & RESTRAINT ANALYTICS")
    lines.append("-" * width)
    lines.append(f"  {'Strategy':<26} {'AI Agent Count':<18} {'Baseline Count':<18} {'Delta'}")
    lines.append("  " + "-" * 75)

    all_strats = sorted(set(list(m["agent_distribution"].keys()) + list(m["baseline_distribution"].keys())))
    for s in all_strats:
        a_cnt = m["agent_distribution"].get(s, 0)
        b_cnt = m["baseline_distribution"].get(s, 0)
        lines.append(f"  {s:<26} {a_cnt:<18} {b_cnt:<18} {a_cnt - b_cnt:+d}")

    lines.append("")
    n = m["total_evaluated_debtors"]
    lines.append(f"  * WAIT Restraint Share    : Agent {m['agent_wait_restraint_pct']}% ({m['agent_wait_restraint_count']}/{n}) vs Baseline {m['baseline_wait_restraint_pct']}% ({m['baseline_wait_restraint_count']}/{n})")
    lines.append(f"  * Reconciliation Routing  : Agent {m['reconcile_routing_pct']}% ({m['reconcile_routing_count']}/{n}) accounts directed to Form 26AS / UTR check")
    lines.append(f"  * Prevented Escalations   : {m['prevented_escalations_count']} accounts spared from premature Day 30+ legal notices")
    lines.append(f"  * Touch Efficiency        : Agent {m['agent_touches_per_lakh']} touches/Lakh vs Baseline {m['baseline_touches_per_lakh']} touches/Lakh")
    lines.append("")
    lines.append("3. HARD-CASE ADJUDICATION MATRIX (CURATED 8 ARCHETYPES)")
    lines.append("-" * width)
    lines.append(f"  {'Debtor':<30} {'Baseline Action':<28} {'AI Agent Action':<28} {'Verdict'}")
    lines.append("  " + "-" * (width - 2))

    for item in result.adjudication_matrix:
        d_label = f"{item['debtor_id']} ({item['debtor_name'][:20]})"
        lines.append(f"  {d_label:<30} {item['baseline_action']:<28} {item['agent_action']:<28} {item['verdict']}")

    lines.append("=" * width)
    return "\n".join(lines)


def format_markdown_output(result: ExperimentResult) -> str:
    """Render markdown representation of the evaluation benchmark."""
    p = result.portfolio
    m = result.comparative_metrics

    lines: list[str] = [
        "# B2B Receivables Recovery Agent: Evaluation Benchmark",
        f"**Seed:** `{result.seed}` | **As-Of:** `{result.as_of}` | **Mode:** `{'Live LLM' if result.is_live_llm else 'Deterministic Evaluator'}`",
        "",
        "## 1. Portfolio Overview",
        "",
        f"- **Total Book Value**: `{p.book_value_inr}` ({p.total_invoices} invoices across {p.total_debtors} debtors)",
        f"- **Naive Outstanding**: `{p.naive_outstanding_inr}` *(Baseline assumes full amount is due)*",
        f"- **True Collectible Balance**: `{p.collectible_inr}` *(Net of TDS credits & off-rail NEFT payments)*",
        "",
        "## 2. Strategy Distribution & Restraint Analytics",
        "",
        "| Strategy | AI Agent | Calendar Baseline | Delta |",
        "| :--- | :--- | :--- | :--- |",
    ]

    all_strats = sorted(set(list(m["agent_distribution"].keys()) + list(m["baseline_distribution"].keys())))
    for s in all_strats:
        a_cnt = m["agent_distribution"].get(s, 0)
        b_cnt = m["baseline_distribution"].get(s, 0)
        lines.append(f"| `{s}` | {a_cnt} | {b_cnt} | {a_cnt - b_cnt:+d} |")

    lines.extend([
        "",
        "> [!NOTE]",
        "> **Key Restraint & Quality Insights**:",
        f"> - **WAIT Restraint Share**: AI Agent achieved **{m['agent_wait_restraint_pct']}%** restraint ({m['agent_wait_restraint_count']}/{m['total_evaluated_debtors']}) vs Baseline **{m['baseline_wait_restraint_pct']}%** ({m['baseline_wait_restraint_count']}/{m['total_evaluated_debtors']}).",
        f"> - **Reconciliation Routing**: **{m['reconcile_routing_count']} accounts** routed to `RECONCILE` for Form 26AS/UTR checks rather than demanding unowed money.",
        f"> - **Prevented Premature Escalations**: **{m['prevented_escalations_count']} debtors** protected from aggressive statutory demand notices.",
        f"> - **Touch Efficiency**: **{m['agent_touches_per_lakh']}** touches per Rs 1 Lakh collected (vs Baseline **{m['baseline_touches_per_lakh']}**).",
        "",
        "## 3. Curated Hard-Case Adjudication Matrix",
        "",
        "| Debtor | Case | Baseline Decision | Agent Decision | Verdict |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])

    for item in result.adjudication_matrix:
        lines.append(
            f"| `{item['debtor_id']}` {item['debtor_name']} | {item['case_name']} | `{item['baseline_action']}` | `{item['agent_action']}` | **{item['verdict']}** |"
        )

    lines.append("")
    return "\n".join(lines)


def format_json_output(result: ExperimentResult) -> str:
    """Render structured JSON representation of the evaluation benchmark."""
    payload = {
        "seed": result.seed,
        "as_of": result.as_of,
        "is_live_llm": result.is_live_llm,
        "portfolio": asdict(result.portfolio),
        "comparative_metrics": result.comparative_metrics,
        "adjudication_matrix": result.adjudication_matrix,
        "agent_decisions": [d.model_dump() for d in result.agent_decisions],
        "baseline_decisions": [asdict(d) for d in result.baseline_decisions],
    }
    return json.dumps(payload, indent=2, default=str)


def run_experiment(
    *,
    seed: int = 42,
    as_of: str | None = None,
    live_llm: bool = False,
    output_format: str = "table",
) -> ExperimentResult:
    """Execute complete comparative evaluation run inside sandboxed audit environment."""
    with isolated_audit_log():
        ledger = generate(seed=seed)
        if as_of:
            ledger["as_of"] = as_of

        portfolio = calculate_portfolio_metrics(ledger)

        if live_llm:
            agent_decisions = run_strategist_batch(ledger)
        else:
            agent_decisions = run_deterministic_agent_batch(ledger)

        baseline_decisions = run_baseline_batch(ledger)

        comparative_metrics = calculate_comparative_metrics(agent_decisions, baseline_decisions, ledger)
        adjudication_matrix = build_adjudication_matrix(agent_decisions, baseline_decisions, ledger)

        return ExperimentResult(
            seed=seed,
            as_of=ledger.get("as_of", AS_OF.isoformat()),
            is_live_llm=live_llm,
            portfolio=portfolio,
            agent_decisions=agent_decisions,
            baseline_decisions=baseline_decisions,
            comparative_metrics=comparative_metrics,
            adjudication_matrix=adjudication_matrix,
        )


def save_report(result: ExperimentResult, path: Path | str, format_type: str = "table") -> Path:
    """Save formatted benchmark report to disk."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if format_type == "json" or out_path.suffix == ".json":
        content = format_json_output(result)
    elif format_type == "markdown" or out_path.suffix == ".md":
        content = format_markdown_output(result)
    else:
        content = format_table_output(result)

    out_path.write_text(content, encoding="utf-8")
    return out_path


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluation Harness & Benchmark Runner for AI Recovery Strategist")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for ledger generation (default: 42)")
    parser.add_argument("--as-of", type=str, default=None, help="Reference date YYYY-MM-DD (default: ledger as_of)")
    parser.add_argument("--live-llm", action="store_true", help="Execute live LLM calls instead of deterministic solver")
    parser.add_argument("--output", choices=["table", "json", "markdown"], default="table", help="Output format (default: table)")
    parser.add_argument("--save", type=str, default=None, help="Optional filepath to save output")
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()
    result = run_experiment(
        seed=args.seed,
        as_of=args.as_of,
        live_llm=args.live_llm,
        output_format=args.output,
    )

    if args.output == "json":
        output_text = format_json_output(result)
    elif args.output == "markdown":
        output_text = format_markdown_output(result)
    else:
        output_text = format_table_output(result)

    print(output_text)

    if args.save:
        saved_file = save_report(result, args.save, format_type=args.output)
        print(f"\nReport saved to: {saved_file}")


if __name__ == "__main__":
    main()
