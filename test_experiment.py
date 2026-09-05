"""Test suite for Evaluation Harness & Benchmark Runner (run_experiment.py).

Strict TDD: Tests written before implementation.
Verifies:
1. Portfolio metrics and comparative metrics calculation.
2. Restraint share, reconciliation routing, prevented escalations, touch efficiency.
3. 8 Hard-Case Adjudication Matrix extraction.
4. Output formatters (table, markdown, json) and file saving.
5. CLI argument parsing and sandboxed audit execution.
"""

from __future__ import annotations

import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from app.baseline import BaselineDecision, run_baseline_batch
from app.envelope import Channel, Language, Strategy, Tone
from app.ledger import generate, invoice_collectible_paise
from app.strategist import StrategistDecision


class TestExperimentRunner(unittest.TestCase):
    def setUp(self) -> None:
        # Every test that runs a decision batch must fold a clean audit log, not the
        # developer's live audit/events.jsonl -- otherwise the restraint and routing
        # assertions below are satisfied by decisions shaped by dev residue and pass
        # only on this machine. The runner isolates internally; the batch helpers the
        # tests call directly do not, so isolation is established here for all of them.
        from run_experiment import isolated_audit_log

        self.enterContext(isolated_audit_log())
        self.ledger = generate(seed=42)

    def test_portfolio_metrics_calculation(self) -> None:
        """Verify portfolio totals for book value, naive outstanding, and collectible balance."""
        from app import audit
        from run_experiment import calculate_portfolio_metrics

        # setUp's isolation must actually be in effect -- without this assertion, deleting
        # the enterContext line would silently send the whole suite back to folding the
        # developer's audit/events.jsonl and it would still pass.
        self.assertIn("recovery-experiment-audit-", str(audit.get_audit_dir()))

        metrics = calculate_portfolio_metrics(self.ledger)
        self.assertEqual(metrics.total_debtors, 20)
        self.assertEqual(metrics.total_invoices, 70)
        self.assertGreater(metrics.total_book_value_paise, 0)
        self.assertGreater(metrics.total_naive_outstanding_paise, 0)
        self.assertGreater(metrics.total_collectible_paise, 0)
        # Collectible must be <= naive outstanding (because TDS withheld and off-rail are credited)
        self.assertLessEqual(metrics.total_collectible_paise, metrics.total_naive_outstanding_paise)
        self.assertLessEqual(metrics.total_naive_outstanding_paise, metrics.total_book_value_paise)

    def test_strategy_distribution_and_restraint_analytics(self) -> None:
        """Verify comparative metrics: strategy distribution, restraint share, reconciliation routing, touch efficiency."""
        from run_experiment import (
            calculate_comparative_metrics,
            run_deterministic_agent_batch,
        )

        agent_decisions = run_deterministic_agent_batch(self.ledger)
        baseline_decisions = run_baseline_batch(self.ledger)

        total_collectible = sum(invoice_collectible_paise(i) for i in self.ledger["invoices"])
        comp = calculate_comparative_metrics(agent_decisions, baseline_decisions, total_collectible)
        self.assertIn("agent_distribution", comp)
        self.assertIn("baseline_distribution", comp)
        # Baseline always has 0% WAIT restraint on overdue debtors
        self.assertEqual(comp["baseline_wait_restraint_count"], 0)
        # Agent exhibits genuine WAIT restraint on reliable late payers / opt-outs
        self.assertGreaterEqual(comp["agent_wait_restraint_count"], 1)
        self.assertGreater(comp["agent_wait_restraint_pct"], 0.0)
        # Reconciliation routing
        self.assertGreaterEqual(comp["reconcile_routing_count"], 1)
        # Prevented escalations
        self.assertGreater(comp["prevented_escalations_count"], 0)
        # Touch efficiency
        self.assertIn("agent_touches_per_lakh", comp)
        self.assertIn("baseline_touches_per_lakh", comp)
        self.assertLess(comp["agent_touches_per_lakh"], comp["baseline_touches_per_lakh"])
        # The displayed percentage must divide by the same count that is displayed with it.
        self.assertEqual(comp["total_evaluated_debtors"], len(agent_decisions))
        self.assertEqual(comp["baseline_wait_restraint_pct"], 0.0)

    def test_restraint_percentage_denominator_matches_evaluated_debtors(self) -> None:
        """total_evaluated_debtors must equal len(agent_decisions), not len(ledger['debtors']).

        Seed 3 has a debtor with no open invoices, so the two counts diverge (19 vs 20) --
        reproducing the mismatch a fixed seed-42 test cannot catch.
        """
        from run_experiment import (
            calculate_comparative_metrics,
            run_deterministic_agent_batch,
        )

        ledger = generate(seed=3)
        agent_decisions = run_deterministic_agent_batch(ledger)
        baseline_decisions = run_baseline_batch(ledger)
        self.assertLess(len(agent_decisions), len(ledger["debtors"]))

        total_collectible = sum(invoice_collectible_paise(i) for i in ledger["invoices"])
        comp = calculate_comparative_metrics(agent_decisions, baseline_decisions, total_collectible)
        self.assertEqual(comp["total_evaluated_debtors"], len(agent_decisions))

    def test_prevented_escalations_pairs_by_debtor_id_not_position(self) -> None:
        """A positional zip would misattribute decisions when the two lists' orders differ."""
        from run_experiment import calculate_comparative_metrics

        def agent(debtor_id: str, strategy: Strategy) -> StrategistDecision:
            return StrategistDecision(
                debtor_id=debtor_id,
                debtor_name=debtor_id,
                strategy=strategy,
                channel=Channel.NONE,
                language=Language.EN,
                tone=Tone.NEUTRAL,
                reasoning="test",
                confidence=1.0,
            )

        def baseline(debtor_id: str, strategy: Strategy) -> BaselineDecision:
            return BaselineDecision(
                debtor_id=debtor_id,
                debtor_name=debtor_id,
                strategy=strategy,
                channel="email",
                tone="firm",
                ask_amount_paise=0,
                days_overdue=40,
                reasoning="test",
            )

        agent_decisions = [
            agent("DEB-1", Strategy.ESCALATE),
            agent("DEB-2", Strategy.WAIT),
        ]
        # Deliberately reversed relative to agent_decisions.
        baseline_decisions = [
            baseline("DEB-2", Strategy.ESCALATE),
            baseline("DEB-1", Strategy.REQUEST_PAYMENT),
        ]

        comp = calculate_comparative_metrics(agent_decisions, baseline_decisions, 0)
        # Correct (id-paired): DEB-1 agent=ESCALATE (not prevented), DEB-2 baseline=ESCALATE
        # and agent=WAIT (prevented) -> 1. A positional zip pairs DEB-1-agent with
        # DEB-2-baseline and DEB-2-agent with DEB-1-baseline, both of which look
        # "not prevented" by coincidence of this data, giving 0.
        self.assertEqual(comp["prevented_escalations_count"], 1)

    def test_curated_hard_case_adjudication_matrix(self) -> None:
        """Verify 8 hard-case adjudication matrix covering all debtor archetypes."""
        from run_experiment import (
            build_adjudication_matrix,
            run_deterministic_agent_batch,
        )

        agent_decisions = run_deterministic_agent_batch(self.ledger)
        baseline_decisions = run_baseline_batch(self.ledger)

        matrix = build_adjudication_matrix(agent_decisions, baseline_decisions, self.ledger)
        self.assertGreaterEqual(len(matrix), 6)
        self.assertLessEqual(len(matrix), 8)

        case_names = [item["case_name"].lower() for item in matrix]
        # Verify key archetypes are present
        self.assertTrue(any("tds" in name for name in case_names), "TDS Deducted archetype missing")
        self.assertTrue(any("off-rail" in name or "neft" in name for name in case_names), "Off-Rail NEFT archetype missing")
        self.assertTrue(any("dispute" in name for name in case_names), "Active Dispute archetype missing")
        self.assertTrue(any("trader" in name or "ineligible" in name for name in case_names), "Trader Ineligible archetype missing")
        self.assertTrue(any("vip" in name for name in case_names), "VIP Relationship archetype missing")
        self.assertTrue(any("opt-out" in name for name in case_names), "Opt-Out archetype missing")
        self.assertTrue(any("reliable" in name or "cooldown" in name for name in case_names), "Reliable Late Payer archetype missing")
        self.assertTrue(any("human" in name or "loss" in name or "review" in name for name in case_names), "Human Review / Loss archetype missing")

        # Every verdict shown must be earned: the agent's strategy differs from baseline's,
        # is not "N/A", AND is one of the strategies the shipped case table says this
        # archetype is about -- checked against the real _DEFAULT_ADJUDICATION_CASES, not
        # just the injected case in the next test.
        from run_experiment import _DEFAULT_ADJUDICATION_CASES

        expected_by_id = {c["case_id"]: c["expected_agent_strategies"] for c in _DEFAULT_ADJUDICATION_CASES}
        for item in matrix:
            if item["verdict"].startswith("N/A"):
                continue
            self.assertNotEqual(item["agent_strategy"], item["baseline_strategy"])
            self.assertNotEqual(item["agent_strategy"], "N/A")
            self.assertIn(item["agent_strategy"], expected_by_id[item["case_id"]])

        # At seed 42 every one of the 8 curated archetypes genuinely reproduces -- including
        # the four (4, 5, 6, 7) whose state+strategy signature alone would also be satisfied
        # by the wrong mechanism, so this also exercises mechanism_check on the real cases,
        # not just the injected ones above.
        non_na_case_ids = {item["case_id"] for item in matrix if not item["verdict"].startswith("N/A")}
        self.assertEqual(non_na_case_ids, {1, 2, 3, 4, 5, 6, 7, 8})

    def test_verdict_is_na_when_agent_strategy_does_not_match_the_archetype(self) -> None:
        """A divergent-but-wrong agent strategy must not inherit the case's authored verdict.

        The baseline escalates the whole book, so "agent != baseline" is true for almost
        every debtor. The verdict gate must also require the agent to have done the thing
        the archetype is about.
        """
        from run_experiment import build_adjudication_matrix, run_deterministic_agent_batch

        agent_decisions = run_deterministic_agent_batch(self.ledger)
        baseline_decisions = run_baseline_batch(self.ledger)

        # DEB-004 holds a TDS_UNDERPAID invoice and the agent reconciles it (not WAIT).
        # RECONCILE diverges from the baseline's ESCALATE, yet a case demanding WAIT here
        # must still fall to N/A because RECONCILE is not what this archetype demonstrates.
        mislabelled = [{
            "case_id": 99,
            "case_name": "Mislabelled archetype",
            "debtor_id": "DEB-004",
            "target_invoice_state": "TDS_UNDERPAID",
            "expected_agent_strategies": {"WAIT"},
            "rationale": "authored",
            "verdict": "Agent Win (must not be shown)",
        }]
        matrix = build_adjudication_matrix(
            agent_decisions, baseline_decisions, self.ledger, cases=mislabelled
        )
        self.assertTrue(matrix[0]["verdict"].startswith("N/A"))

    def test_verdict_is_na_when_state_and_strategy_match_but_mechanism_does_not(self) -> None:
        """A right-strategy-wrong-reason decision must not inherit the case's verdict either.

        DEB-017 is a reliable late payer, not an opt-out -- it reaches WAIT via the mock's
        own is_reliable_late_payer branch, never the opt-out fast path. Case 6's OVERDUE +
        WAIT gate signature is identical to Case 7's, so before mechanism_check existed this
        would have shown a fabricated "Zero harassment compliance" win for a debtor who
        never opted out of anything.
        """
        from run_experiment import (
            _opted_out_mechanism,
            build_adjudication_matrix,
            run_deterministic_agent_batch,
        )

        agent_decisions = run_deterministic_agent_batch(self.ledger)
        baseline_decisions = run_baseline_batch(self.ledger)

        mislabelled = [{
            "case_id": 98,
            "case_name": "Mislabelled mechanism",
            "debtor_id": "DEB-017",
            "target_invoice_state": "OVERDUE",
            "expected_agent_strategies": {"WAIT"},
            "mechanism_check": _opted_out_mechanism,
            "rationale": "authored",
            "verdict": "Agent Win (must not be shown)",
        }]
        matrix = build_adjudication_matrix(
            agent_decisions, baseline_decisions, self.ledger, cases=mislabelled
        )
        self.assertTrue(matrix[0]["verdict"].startswith("N/A"))

    def test_adjudication_verdict_is_na_when_case_does_not_reproduce(self) -> None:
        """A debtor missing the case's target invoice state must not show a fabricated win.

        Seed 100 reproduces exactly this: DEB-005 has no open invoices at all, yet the
        case list still names it for the Off-Rail NEFT archetype.
        """
        from run_experiment import build_adjudication_matrix, run_deterministic_agent_batch

        ledger = generate(seed=100)
        agent_decisions = run_deterministic_agent_batch(ledger)
        baseline_decisions = run_baseline_batch(ledger)
        matrix = build_adjudication_matrix(agent_decisions, baseline_decisions, ledger)

        row = next(item for item in matrix if item["debtor_id"] == "DEB-005")
        self.assertTrue(row["verdict"].startswith("N/A"))

    def test_output_formatters(self) -> None:
        """Verify Table, JSON, and Markdown output generation."""
        from run_experiment import (
            format_json_output,
            format_markdown_output,
            format_table_output,
            run_experiment,
        )

        result = run_experiment(seed=42, live_llm=False)

        # Table formatting
        table_out = format_table_output(result)
        self.assertIn("PORTFOLIO OVERVIEW", table_out)
        self.assertIn("STRATEGY DISTRIBUTION", table_out)
        self.assertIn("HARD-CASE ADJUDICATION MATRIX", table_out)
        # The default run is the scripted mock, not the live model; the table report (the
        # CLI's default output) must say so, not only the markdown report.
        self.assertIn("Deterministic Evaluator", table_out)

        # Markdown formatting
        md_out = format_markdown_output(result)
        self.assertIn("# PayUpPal: Evaluation Benchmark", md_out)
        self.assertIn("| Debtor | Case | Baseline Decision | Agent Decision | Verdict |", md_out)

        # JSON formatting
        json_out = format_json_output(result)
        parsed = json.loads(json_out)
        self.assertIn("portfolio", parsed)
        self.assertIn("comparative_metrics", parsed)
        self.assertIn("adjudication_matrix", parsed)

    def test_save_report(self) -> None:
        """Verify saving report to specified file path."""
        from run_experiment import run_experiment, save_report

        result = run_experiment(seed=42, live_llm=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "report.md"
            saved_path = save_report(result, out_file, format_type="markdown")
            self.assertTrue(saved_path.exists())
            self.assertIn("Evaluation Benchmark", saved_path.read_text(encoding="utf-8"))

    def test_cli_parsing(self) -> None:
        """Verify CLI argument parser configurations."""
        from run_experiment import parse_args

        args = parse_args(["--seed", "100", "--output", "json", "--live-llm"])
        self.assertEqual(args.seed, 100)
        self.assertEqual(args.output, "json")
        self.assertTrue(args.live_llm)

    def test_deterministic_batch_does_not_mutate_llm_complete(self) -> None:
        """Verify run_deterministic_agent_batch preserves global llm.complete function identity."""
        from app import llm
        from run_experiment import run_deterministic_agent_batch

        orig_complete = llm.complete
        run_deterministic_agent_batch(self.ledger)
        self.assertIs(llm.complete, orig_complete)

    def test_isolated_audit_log_thread_safety(self) -> None:
        """Verify isolated_audit_log in one thread does not redirect writes in other threads."""
        from app import audit
        from run_experiment import isolated_audit_log

        def background_thread_audit_write() -> tuple[Path, Path]:
            # Thread without isolated_audit_log must see default production paths
            return audit.get_audit_dir(), audit.get_event_log()

        with isolated_audit_log():
            isolated_dir = audit.get_audit_dir()
            self.assertIn("recovery-experiment-audit-", str(isolated_dir))

            # Run in a separate thread pool worker
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                bg_dir, bg_log = pool.submit(background_thread_audit_write).result()

            self.assertEqual(bg_dir, audit.AUDIT_DIR)
            self.assertEqual(bg_log, audit.EVENT_LOG)
            self.assertNotEqual(bg_dir, isolated_dir)


if __name__ == "__main__":
    unittest.main()
