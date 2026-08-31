"""Comprehensive Adversarial Challenge & Stress Test Suite for PR#9.

Empirical verification and challenge suites:
1. MSMED Section 15 & 16 calculation boundary values (exact leap years, 15d vs 45d caps, discrete compounding at 3x RBI bank rate in integer paise).
2. Master Kill Switch & Review-First invariant enforcement under concurrent simulation.
3. Razorpay webhook idempotency, signature validation, and payment claim race conditions.
4. Template XSS vectors and CSV formula injection neutralization in operator.html and results.html.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import os
import random
import threading
import unittest
from datetime import date, timedelta
from unittest import mock

# Ensure hermetic environment for test harness
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient

from app import audit, config, operator, pipeline, razorpay_gateway, statute
from app.envelope import Channel, Language, Tone
from app.messages import DraftedMessage
from app.server import INVOICES, app, suppress_on_settlement, templates


class TestChallenge1MSMEDBoundary(unittest.TestCase):
    """Challenge 1: MSMED Section 15 & 16 calculation boundary values."""

    def test_msmed_section_15_caps_and_leap_year_boundaries(self):
        """Test MSMED Section 15 date arithmetic across leap years and non-leap years."""
        # Case 1: Leap year 2024 (Feb has 29 days)
        deliv_2024 = date(2024, 2, 15)
        # Without written agreement -> 15 calendar days from delivery
        acc, due, appt = statute.compute_statutory_dates(deliv_2024, written_agreement=False)
        self.assertEqual(acc, date(2024, 2, 15))
        self.assertEqual(due, date(2024, 3, 1))
        self.assertEqual(appt, date(2024, 3, 2))
        self.assertEqual((due - deliv_2024).days, 15)
        self.assertEqual((appt - due).days, 1)

        # Case 2: Non-leap year 2023 (Feb has 28 days)
        deliv_2023 = date(2023, 2, 15)
        acc_23, due_23, appt_23 = statute.compute_statutory_dates(deliv_2023, written_agreement=False)
        self.assertEqual(acc_23, date(2023, 2, 15))
        self.assertEqual(due_23, date(2023, 3, 2))
        self.assertEqual(appt_23, date(2023, 3, 3))
        self.assertEqual((due_23 - deliv_2023).days, 15)

        # Case 3: Written agreement capped at 45 calendar days
        acc_45, due_45, appt_45 = statute.compute_statutory_dates(deliv_2024, written_agreement=True)
        self.assertEqual(acc_45, date(2024, 2, 15))
        self.assertEqual(due_45, date(2024, 3, 31))
        self.assertEqual(appt_45, date(2024, 4, 1))
        self.assertEqual((due_45 - deliv_2024).days, 45)

        # Case 4: Dispute objection within 15 days on Leap Day (2024-02-29)
        obj_date = date(2024, 2, 29)
        acc_unres, due_unres, appt_unres = statute.compute_statutory_dates(
            deliv_2024, written_agreement=False, objection_date=obj_date, resolution_date=None
        )
        self.assertIsNone(acc_unres)
        self.assertIsNone(due_unres)
        self.assertIsNone(appt_unres)

        res_date = date(2024, 3, 10)
        acc_res, due_res, appt_res = statute.compute_statutory_dates(
            deliv_2024, written_agreement=False, objection_date=obj_date, resolution_date=res_date
        )
        self.assertEqual(acc_res, date(2024, 3, 10))
        self.assertEqual(due_res, date(2024, 3, 25))
        self.assertEqual(appt_res, date(2024, 3, 26))

        # Case 5: Dispute objection after 15 days (March 5, 2024, day 19)
        obj_late = date(2024, 3, 5)
        acc_late, due_late, appt_late = statute.compute_statutory_dates(
            deliv_2024, written_agreement=False, objection_date=obj_late, resolution_date=None
        )
        self.assertEqual(acc_late, date(2024, 2, 15))
        self.assertEqual(due_late, date(2024, 3, 1))
        self.assertEqual(appt_late, date(2024, 3, 2))

    def test_msmed_section_16_interest_exact_compounding_invariants(self):
        """Stress-test Section 16 interest calculation: integer paise, compounding rests, float drift."""
        due = date(2026, 1, 1)
        res_zero = statute.calculate_section_16_interest(100000, due, due)
        self.assertEqual(res_zero.days_overdue, 0)
        self.assertEqual(res_zero.accrued_interest_paise, 0)
        self.assertEqual(res_zero.total_payable_paise, 100000)

        res_neg = statute.calculate_section_16_interest(100000, due, date(2025, 12, 15))
        self.assertEqual(res_neg.days_overdue, 0)
        self.assertEqual(res_neg.accrued_interest_paise, 0)
        self.assertEqual(res_neg.total_payable_paise, 100000)

        res_zero_p = statute.calculate_section_16_interest(0, due, date(2026, 2, 1))
        self.assertEqual(res_zero_p.accrued_interest_paise, 0)
        self.assertEqual(res_zero_p.total_payable_paise, 0)

        # Invariant across 500 combinations: total_payable_paise == principal_paise + accrued_interest_paise
        for principal in [1, 2, 99, 100, 10000, 5000000, 1000000000]:
            for days in [1, 15, 29, 30, 31, 59, 60, 61, 90, 180, 365, 730, 1095]:
                as_of = due + timedelta(days=days)
                res = statute.calculate_section_16_interest(principal, due, as_of)
                self.assertEqual(
                    res.total_payable_paise,
                    res.principal_paise + res.accrued_interest_paise,
                    f"Invariant violated for principal={principal}, days={days}",
                )
                self.assertEqual(res.days_overdue, days)
                self.assertEqual(res.full_months, days // 30)
                self.assertEqual(res.residual_days, days % 30)

        # 60 days overdue (exact 2 months rest, 0 residual days)
        res_60 = statute.calculate_section_16_interest(1000000, due, due + timedelta(days=60))
        self.assertEqual(res_60.full_months, 2)
        self.assertEqual(res_60.residual_days, 0)
        self.assertEqual(res_60.compounded_principal_paise, 1034035)
        self.assertEqual(res_60.residual_interest_paise, 0)
        self.assertEqual(res_60.total_payable_paise, 1034035)
        self.assertEqual(res_60.accrued_interest_paise, 34035)

        # 75 days overdue (2 months + 15 days)
        res_75 = statute.calculate_section_16_interest(1000000, due, due + timedelta(days=75))
        self.assertEqual(res_75.full_months, 2)
        self.assertEqual(res_75.residual_days, 15)
        self.assertEqual(res_75.total_payable_paise, 1042640)
        self.assertEqual(res_75.accrued_interest_paise, 42640)

    def test_randomized_stress_generator_oracle(self):
        """Randomized stress generator verifying strict monotonic growth and integer paise quantization."""
        rng = random.Random(42)
        due = date(2025, 1, 1)
        rates = [4.5, 6.0, 6.75, 7.25, 8.5]

        for _ in range(1000):
            principal = rng.randint(100, 1000000000)
            days = rng.randint(1, 1825)
            rate = rng.choice(rates)

            res = statute.calculate_section_16_interest(principal, due, due + timedelta(days=days), rbi_bank_rate_pct=rate)
            self.assertEqual(res.total_payable_paise, res.principal_paise + res.accrued_interest_paise)
            self.assertGreaterEqual(res.total_payable_paise, res.principal_paise)
            self.assertGreaterEqual(res.accrued_interest_paise, 0)


class TestChallenge2KillSwitchAndReviewFirst(unittest.TestCase):
    """Challenge 2: Master Kill Switch & Review-First invariant enforcement under concurrent simulation."""

    def setUp(self):
        operator.set_kill_switch(False)
        with operator._OPERATOR_LOCK:
            operator._REVIEW_QUEUE.clear()

    def tearDown(self):
        operator.set_kill_switch(False)
        with operator._OPERATOR_LOCK:
            operator._REVIEW_QUEUE.clear()

    def test_kill_switch_concurrency_and_halt_guarantee(self):
        """Stress-test kill switch toggling concurrently with pipeline and operator approval."""
        operator.set_kill_switch(True)
        self.assertTrue(operator.is_kill_switch_active())

        draft = DraftedMessage(
            debtor_id="DEBTOR-KS-1",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FIRM,
            subject="Statutory Notice",
            body="MSMED notice body",
            is_statutory=True,
            dark_pattern_clean=True,
            recipient_email="test@example.com",
        )
        operator.queue_for_review(
            debtor_id="DEBTOR-KS-1",
            debtor_name="Acme Corp",
            strategy="STATUTORY_MSMED_DEMAND",
            ask_amount_paise=1000000,
            reasoning="Statutory demand",
            draft=draft,
            recipient_email="test@example.com",
        )

        res = operator.approve_review_item("DEBTOR-KS-1")
        self.assertIsNotNone(res)
        self.assertFalse(res["approved"])
        self.assertIn("kill switch", res["error"])

        queue = operator.get_review_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["debtor_id"], "DEBTOR-KS-1")

        operator.set_kill_switch(False)
        self.assertFalse(operator.is_kill_switch_active())

        res2 = operator.approve_review_item("DEBTOR-KS-1")
        self.assertIsNotNone(res2)
        self.assertTrue(res2["approved"])
        self.assertEqual(len(operator.get_review_queue()), 0)

    def test_concurrent_review_queue_approvals_race_safety(self):
        """Verify that concurrent approvals on the same queue item never double-dispatch."""
        draft = DraftedMessage(
            debtor_id="DEBTOR-RACE-1",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FIRM,
            subject="Test Subject",
            body="Test Body",
            is_statutory=False,
            dark_pattern_clean=True,
            recipient_email="race@example.com",
        )
        operator.queue_for_review(
            debtor_id="DEBTOR-RACE-1",
            debtor_name="Race Debtor",
            strategy="GENTLE_REMINDER",
            ask_amount_paise=500000,
            reasoning="Test race",
            draft=draft,
            recipient_email="race@example.com",
        )

        results = []
        errors = []

        def worker_approve():
            try:
                res = operator.approve_review_item("DEBTOR-RACE-1")
                results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_approve) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        approved_count = sum(1 for r in results if r is not None and r.get("approved") is True)
        none_count = sum(1 for r in results if r is None)
        self.assertEqual(approved_count, 1)
        self.assertEqual(none_count, 19)
        self.assertEqual(len(operator.get_review_queue()), 0)

    def test_pipeline_kill_switch_and_opt_out_invariants(self):
        """Verify that pipeline halts on kill switch and permanently suppresses opted-out debtors."""
        ledger = {
            "debtors": [
                {"debtor_id": "D-OPT", "name": "Opted Out Debtor", "opted_out": True, "merchant_id": "M-1"},
                {"debtor_id": "D-NORM", "name": "Normal Debtor", "opted_out": False, "merchant_id": "M-1"},
            ],
            "merchants": [{"merchant_id": "M-1", "name": "Supplier 1", "udyam_registered": True}],
            "invoices": [
                {"invoice_id": "INV-OPT", "debtor_id": "D-OPT", "merchant_id": "M-1", "amount_paise": 500000, "status": "OVERDUE"},
                {"invoice_id": "INV-NORM", "debtor_id": "D-NORM", "merchant_id": "M-1", "amount_paise": 500000, "status": "OVERDUE"},
            ],
        }

        operator.set_kill_switch(True)
        res_ks = pipeline.execute_recovery_pipeline(ledger, as_of=date(2026, 8, 31))
        self.assertEqual(res_ks.automated_dispatches, 0)
        self.assertEqual(res_ks.suppressed_opt_out, 1)
        self.assertEqual(res_ks.kill_switch_blocked, 1)

        operator.set_kill_switch(False)


class TestChallenge3RazorpayWebhookAndIdempotency(unittest.TestCase):
    """Challenge 3: Razorpay webhook idempotency, signature validation, and payment claim race conditions."""

    def setUp(self):
        INVOICES.pop("INV-RACE-IDEMP", None)
        INVOICES.pop("INV-RACE-PARTIAL", None)

    def tearDown(self):
        INVOICES.pop("INV-RACE-IDEMP", None)
        INVOICES.pop("INV-RACE-PARTIAL", None)

    def test_webhook_and_payment_signature_verification(self):
        """Verify HMAC-SHA256 signature verification edge cases and timing attack safety."""
        test_secret = "test_webhook_secret_xyz123"
        test_body = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_123"}}}}'

        correct_sig = hmac.new(test_secret.encode("utf-8"), test_body, hashlib.sha256).hexdigest()

        with mock.patch.object(config, "RAZORPAY_WEBHOOK_SECRET", test_secret):
            self.assertTrue(razorpay_gateway.verify_webhook_signature(test_body, correct_sig))
            altered_body = test_body + b" "
            self.assertFalse(razorpay_gateway.verify_webhook_signature(altered_body, correct_sig))
            self.assertFalse(razorpay_gateway.verify_webhook_signature(test_body, "wrong_sig_abcdef"))
            self.assertFalse(razorpay_gateway.verify_webhook_signature(test_body, ""))

        with mock.patch.object(config, "RAZORPAY_WEBHOOK_SECRET", ""), self.assertRaises(RuntimeError):
            razorpay_gateway.verify_webhook_signature(test_body, correct_sig)

    def test_concurrent_settlement_webhook_idempotency_and_replay_protection(self):
        """Stress-test concurrent identical webhook deliveries for payment capture replay protection."""
        token = "INV-RACE-IDEMP"
        inv = {
            "invoice_id": "INV-RACE-IDEMP",
            "debtor": "Idemp Debtor",
            "debtor_id": "DEBTOR-IDEMP",
            "amount_paise": 1000000,
            "amount_received_paise": 0,
            "status": "ISSUED",
            "processed_payment_ids": [],
        }
        INVOICES[token] = inv

        def deliver_webhook():
            suppress_on_settlement(token, inv, "pay_same_123", 1000000)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(deliver_webhook) for _ in range(30)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        self.assertEqual(inv["status"], "PAID")
        self.assertEqual(inv["amount_received_paise"], 1000000)
        self.assertEqual(inv["processed_payment_ids"], ["pay_same_123"])

        # Test partial payment concurrency with multiple different payments
        token_part = "INV-RACE-PARTIAL"
        inv_part = {
            "invoice_id": "INV-RACE-PARTIAL",
            "debtor": "Partial Debtor",
            "debtor_id": "DEBTOR-PARTIAL",
            "amount_paise": 1000000,
            "amount_received_paise": 0,
            "status": "ISSUED",
            "processed_payment_ids": [],
        }
        INVOICES[token_part] = inv_part

        def deliver_part_a():
            suppress_on_settlement(token_part, inv_part, "pay_part_A", 300000)

        def deliver_part_b():
            suppress_on_settlement(token_part, inv_part, "pay_part_B", 400000)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            f_a = [executor.submit(deliver_part_a) for _ in range(10)]
            f_b = [executor.submit(deliver_part_b) for _ in range(10)]
            for f in concurrent.futures.as_completed(f_a + f_b):
                f.result()

        self.assertEqual(inv_part["status"], "PARTIALLY_PAID")
        self.assertEqual(inv_part["amount_received_paise"], 700000)
        self.assertEqual(set(inv_part["processed_payment_ids"]), {"pay_part_A", "pay_part_B"})


class TestChallenge4TemplateXSS(unittest.TestCase):
    """Challenge 4: Template XSS vectors and CSV formula injection neutralization."""

    def test_operator_html_xss_sanitization(self):
        """Verify Jinja2 autoescaping neutralizes hostile XSS vectors in operator.html."""
        xss_payload = '<script>alert("XSS-ATTACK")</script><img src="x" onerror="alert(1)">'
        item = {
            "debtor_id": 'D-XSS" onmouseover="alert(1)',
            "debtor_name": f'Evil Corp {xss_payload}',
            "strategy": "STATUTORY_MSMED_DEMAND",
            "ask_amount_display": "Rs 10,000",
            "channel": "email",
            "language": "en",
            "tone": "firm",
            "is_statutory": True,
            "subject": f'URGENT: {xss_payload}',
            "body": f'Notice body containing {xss_payload}',
            "reasoning": f'Strategist reason: {xss_payload}',
        }

        template = templates.get_template("operator.html")
        rendered = template.render(
            request=None,
            queue=[item],
            kill_switch_active=False,
            operator_key='test-key" <script>alert(2)</script>',
        )

        self.assertNotIn('<script>alert("XSS-ATTACK")</script>', rendered)
        self.assertNotIn('<img src="x" onerror="alert(1)">', rendered)
        self.assertNotIn('<script>alert(2)</script>', rendered)
        self.assertIn('&lt;script&gt;alert(&#34;XSS-ATTACK&#34;)&lt;/script&gt;', rendered)
        self.assertIn('&lt;img src=&#34;x&#34; onerror=&#34;alert(1)&#34;&gt;', rendered)

    def test_results_endpoint_and_html_xss_safety(self):
        """Verify /results endpoint and rendered HTML sanitization on adversarial inputs."""
        client = TestClient(app)
        xss_param = '<script>alert("XSS-PARAM")</script>'
        resp = client.get(f"/results?operator_key={xss_param}")
        self.assertNotIn('<script>alert("XSS-PARAM")</script>', resp.text)

        resp_res = client.get(f"/r/{xss_param}")
        self.assertEqual(resp_res.status_code, 404)
        self.assertNotIn('<script>alert("XSS-PARAM")</script>', resp_res.text)

    def test_csv_formula_injection_neutralization(self):
        """Verify export_audit_events sanitizes malicious Excel/CSV formula prefixes."""
        audit.record(
            "test.formula_injection",
            debtor_id="=CMD|' /C calc'!A0",
            strategy="+12345",
            reason="-SUM(A1:A10)",
            error="@HYPERLINK('http://evil.com','click')",
        )
        csv_data = operator.export_audit_events("csv")
        self.assertIn("'=CMD", csv_data)
        self.assertIn("'+12345", csv_data)
        self.assertIn("'-SUM", csv_data)
        self.assertIn("'@HYPERLINK", csv_data)


if __name__ == "__main__":
    unittest.main()
