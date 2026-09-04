import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.server import app


class TestResultsUIAndAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_get_api_results_structure_and_metrics(self):
        response = self.client.get("/api/results?seed=42")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Verify top-level structure
        self.assertIn("seed", data)
        self.assertIn("as_of", data)
        self.assertIn("portfolio", data)
        self.assertIn("comparative_metrics", data)
        self.assertIn("strategy_distribution", data)
        self.assertIn("adjudication_matrix", data)

        # Verify portfolio metrics
        portfolio = data["portfolio"]
        self.assertEqual(portfolio["total_debtors"], 20)
        self.assertEqual(portfolio["total_invoices"], 70)
        self.assertIn("total_book_value_inr", portfolio)
        self.assertIn("total_collectible_inr", portfolio)

        # Verify strategy distribution deltas
        strat_dist = data["strategy_distribution"]
        self.assertIn("agent", strat_dist)
        self.assertIn("baseline", strat_dist)
        self.assertEqual(strat_dist["agent"].get("WAIT", 0), 2)
        self.assertEqual(strat_dist["baseline"].get("ESCALATE", 0), 20)

        # Verify 8-case adjudication matrix
        matrix = data["adjudication_matrix"]
        self.assertEqual(len(matrix), 8)
        first_case = matrix[0]
        self.assertIn("debtor_id", first_case)
        self.assertIn("debtor_name", first_case)
        self.assertIn("case_name", first_case)
        self.assertIn("baseline_action", first_case)
        self.assertIn("agent_action", first_case)
        self.assertIn("verdict", first_case)
        self.assertIn("drafted_copy_preview", first_case)

    def test_get_api_results_custom_seed_and_as_of(self):
        """A non-default benchmark parameter is a full experiment run, so it needs a key."""
        unauthenticated = self.client.get("/api/results?seed=42&as_of=2026-08-30")
        self.assertEqual(unauthenticated.status_code, 401)

        with patch.dict(os.environ, {"OPERATOR_API_KEY": "test-operator-key"}):
            response = self.client.get(
                "/api/results?seed=42&as_of=2026-08-30",
                headers={"X-Operator-Key": "test-operator-key"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["as_of"], "2026-08-30")

    def test_results_cache_is_bounded(self):
        """The cache key is caller-supplied, so it must not grow without limit."""
        from app.server import _RESULTS_CACHE, _RESULTS_CACHE_MAX

        with patch.dict(os.environ, {"OPERATOR_API_KEY": "test-operator-key"}):
            for seed in range(_RESULTS_CACHE_MAX + 3):
                res = self.client.get(
                    f"/api/results?seed={seed}",
                    headers={"X-Operator-Key": "test-operator-key"},
                )
                self.assertEqual(res.status_code, 200)
        self.assertLessEqual(len(_RESULTS_CACHE), _RESULTS_CACHE_MAX)

    def test_results_html_page_renders_cleanly(self):
        response = self.client.get("/results")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        body = response.text
        self.assertIn("Evaluation Benchmark &amp; Counterfactual", body)
        self.assertIn("Portfolio Overview", body)
        self.assertIn("Strategy Distribution", body)
        self.assertIn("Hard-Case Adjudication Matrix", body)
        self.assertIn("Kaveri Textiles", body)
        self.assertIn("Silverline Interiors", body)
        self.assertIn("Operator Console", body)

    def test_get_api_results_invalid_as_of_returns_400(self):
        response = self.client.get("/api/results?as_of=invalid-date")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid as_of date", response.json()["detail"])

    def test_results_page_invalid_as_of_returns_400(self):
        response = self.client.get("/results?as_of=invalid-date")
        self.assertEqual(response.status_code, 400)

    def test_results_page_forwards_operator_key(self):
        response = self.client.get("/results?key=secret-op-123")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/operator?key=secret-op-123"', response.text)
        self.assertIn('rel="noopener noreferrer"', response.text)

    def test_results_api_does_not_pollute_production_audit_log(self):
        from app import audit
        initial_events_count = len(audit.read_all())
        response = self.client.get("/api/results?seed=42")
        self.assertEqual(response.status_code, 200)
        self.client.get("/results?seed=42")
        final_events_count = len(audit.read_all())
        self.assertEqual(initial_events_count, final_events_count)

    def test_mandate_webhook_invalid_failure_code_returns_422(self):
        response = self.client.post(
            "/api/mandate/simulate-webhook",
            json={"mandate_id": "man_101", "failure_code": "NOT_A_CODE"},
        )
        self.assertEqual(response.status_code, 422)

    def test_live_llm_requires_operator_auth(self):
        # Unauthenticated request for live_llm must return 401
        res_api = self.client.get("/api/results?live_llm=true")
        self.assertEqual(res_api.status_code, 401)
        res_page = self.client.get("/results?live_llm=true")
        self.assertEqual(res_page.status_code, 401)


if __name__ == "__main__":
    unittest.main()


