import unittest
from datetime import date

from app.ledger import generate
from app.operator import set_kill_switch
from app.pipeline import PipelineRunResult, execute_recovery_pipeline


class TestPipelineOrchestration(unittest.TestCase):
    def setUp(self):
        set_kill_switch(False)
        self.ledger = generate(seed=42)

    def test_full_pipeline_run_routes_correctly(self):
        result = execute_recovery_pipeline(self.ledger, as_of=date(2026, 8, 26), dry_run=True)
        self.assertIsInstance(result, PipelineRunResult)
        self.assertEqual(result.total_evaluated, 20)
        self.assertGreater(result.automated_dispatches, 0)
        self.assertGreater(result.review_queued, 0)
        self.assertEqual(result.suppressed_opt_out, 1)

    def test_kill_switch_halts_pipeline_dispatch(self):
        set_kill_switch(True)
        result = execute_recovery_pipeline(self.ledger, as_of=date(2026, 8, 26), dry_run=True)
        self.assertEqual(result.automated_dispatches, 0)
        self.assertGreater(result.kill_switch_blocked, 0)

if __name__ == "__main__":
    unittest.main()
