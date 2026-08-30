import unittest
from datetime import date

from app.mandate import MandateFailureCode, plan_mandate_retries


class TestMandateRetry(unittest.TestCase):
    def test_insufficient_funds_exponential_backoff_cadence(self):
        plan = plan_mandate_retries(
            mandate_id="man_test_123",
            failure_code=MandateFailureCode.INSUFFICIENT_FUNDS,
            failure_date=date(2026, 8, 26),
        )
        self.assertEqual(len(plan.retry_dates), 3)
        self.assertEqual(plan.retry_dates[0], date(2026, 8, 29))  # +3d (salary/fund cycle)
        self.assertEqual(plan.retry_dates[1], date(2026, 9, 3))   # +5d from step 1
        self.assertEqual(plan.retry_dates[2], date(2026, 9, 10))  # +7d from step 2

    def test_expired_mandate_routes_to_fresh_authorization_not_retry(self):
        plan = plan_mandate_retries(
            mandate_id="man_test_456",
            failure_code=MandateFailureCode.MANDATE_EXPIRED,
            failure_date=date(2026, 8, 26),
        )
        self.assertEqual(len(plan.retry_dates), 0)
        self.assertIn("re-authorization", plan.strategy_notes.lower())

    def test_limit_exceeded_and_technical_errors(self):
        plan_limit = plan_mandate_retries(
            mandate_id="man_test_789",
            failure_code=MandateFailureCode.LIMIT_EXCEEDED,
            failure_date=date(2026, 8, 26),
        )
        self.assertEqual(len(plan_limit.retry_dates), 2)

        plan_tech = plan_mandate_retries(
            mandate_id="man_test_000",
            failure_code=MandateFailureCode.TECHNICAL_ERROR,
            failure_date=date(2026, 8, 26),
        )
        self.assertEqual(len(plan_tech.retry_dates), 1)

if __name__ == "__main__":
    unittest.main()
