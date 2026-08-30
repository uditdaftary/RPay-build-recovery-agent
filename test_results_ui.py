import unittest

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
        response = self.client.get("/api/results?seed=42&as_of=2026-08-30")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["as_of"], "2026-08-30")

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
        self.assertIn("Operator Queue", body)


if __name__ == "__main__":
    unittest.main()
