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

import json
import tempfile
import unittest
from pathlib import Path

from app.baseline import run_baseline_batch
from app.ledger import generate


class TestExperimentRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = generate(seed=42)

    def test_portfolio_metrics_calculation(self) -> None:
        """Verify portfolio totals for book value, naive outstanding, and collectible balance."""
        from run_experiment import calculate_portfolio_metrics

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

        comp = calculate_comparative_metrics(agent_decisions, baseline_decisions, self.ledger)
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

        # Markdown formatting
        md_out = format_markdown_output(result)
        self.assertIn("# B2B Receivables Recovery Agent: Evaluation Benchmark", md_out)
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


if __name__ == "__main__":
    unittest.main()
