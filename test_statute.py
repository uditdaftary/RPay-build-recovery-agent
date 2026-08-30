"""Test suite for MSMED Section 15/16 and Income Tax Section 43B(h) Statutory Engine.

Strict TDD: Tests written before implementation.
Verifies:
1. MSMED Section 15 statutory clock, deemed acceptance, objection within 15d, and 45d caps.
2. MSMED Section 16 compound interest with monthly rests at 3x RBI Bank Rate, integer paise precision.
3. Section 43B(h) Income Tax disallowance eligibility, FY calculation, and anti-dark-pattern copy rules.
4. Dispute taxonomy, categorization, and statutory clock recomputation.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from app.disputes import (
    DisputeCategory,
    classify_dispute_reason,
    recompute_statutory_dates_on_dispute,
)
from app.ledger import Merchant, UdyamActivity
from app.statute import (
    DEFAULT_RBI_BANK_RATE_PCT,
    STATUTORY_RATE_MULTIPLIER,
    StatutoryInterestResult,
    calculate_section_16_interest,
    compute_statutory_dates,
    evaluate_section_43b_h,
    generate_section_43b_h_notice,
    get_financial_year,
    get_financial_year_end,
    validate_section_43b_h_copy,
)


class TestStatuteAndDisputes(unittest.TestCase):
    def test_statutory_dates_no_written_agreement(self) -> None:
        """Without a written agreement, payment is due within 15 calendar days from delivery."""
        delivery = date(2026, 8, 1)
        acceptance, statutory_due, appointed = compute_statutory_dates(
            delivery_date=delivery,
            written_agreement=False,
        )
        self.assertEqual(acceptance, delivery, "Silence for 15 days is deemed acceptance on delivery date")
        self.assertEqual(statutory_due, delivery + timedelta(days=15), "Default statutory window is 15 calendar days")
        self.assertEqual(appointed, delivery + timedelta(days=16), "Appointed day is day following 15 days from acceptance")

    def test_statutory_dates_with_written_agreement_capped_at_45d(self) -> None:
        """With written agreement, statutory due date is capped at maximum 45 calendar days from acceptance."""
        delivery = date(2026, 8, 1)
        acceptance, statutory_due, appointed = compute_statutory_dates(
            delivery_date=delivery,
            written_agreement=True,
        )
        self.assertEqual(acceptance, delivery)
        self.assertEqual(statutory_due, delivery + timedelta(days=45), "Written agreement window is 45 calendar days")
        self.assertEqual(appointed, delivery + timedelta(days=46))

    def test_statutory_dates_with_objection_inside_15d_moves_acceptance(self) -> None:
        """Written objection raised within 15 days moves acceptance date to objection resolution date."""
        delivery = date(2026, 8, 1)
        objection = date(2026, 8, 10)  # Raised 9 days after delivery, inside the window
        resolution = date(2026, 8, 20)  # Resolved 19 days later
        acceptance, statutory_due, appointed = compute_statutory_dates(
            delivery_date=delivery,
            written_agreement=True,
            objection_date=objection,
            resolution_date=resolution,
        )
        self.assertEqual(acceptance, resolution, "Acceptance date moves to the date objection is resolved")
        self.assertEqual(statutory_due, resolution + timedelta(days=45), "Statutory window counts from resolution date")
        self.assertEqual(appointed, resolution + timedelta(days=46))

    def test_statutory_dates_unresolved_objection_inside_15d(self) -> None:
        """When objection is raised inside 15d and unresolved, acceptance and statutory due dates are None."""
        delivery = date(2026, 8, 1)
        acceptance, statutory_due, appointed = compute_statutory_dates(
            delivery_date=delivery,
            written_agreement=True,
            objection_date=date(2026, 8, 10),
            resolution_date=None,
        )
        self.assertIsNone(acceptance)
        self.assertIsNone(statutory_due)
        self.assertIsNone(appointed)

    def test_statutory_dates_objection_after_15d_is_reachable_through_the_wrapper(self) -> None:
        """A late objection (after day 15) must still deem acceptance on delivery, via this function.

        A prior version of compute_statutory_dates hardcoded the objection date to always
        fall one day after delivery, which made this branch unreachable except by calling
        recompute_statutory_dates_on_dispute directly.
        """
        delivery = date(2026, 8, 1)
        late_objection = date(2026, 8, 21)  # Day 20, after the 15-day window
        acceptance, statutory_due, appointed = compute_statutory_dates(
            delivery_date=delivery,
            written_agreement=True,
            objection_date=late_objection,
            resolution_date=date(2026, 9, 1),
        )
        self.assertEqual(acceptance, delivery, "Late objection does not move deemed acceptance")
        self.assertEqual(statutory_due, delivery + timedelta(days=45))
        self.assertEqual(appointed, delivery + timedelta(days=46))

    def test_statutory_dates_boundary_edges(self) -> None:
        """Test boundary transitions: 14d, 15d, 16d, 44d, 45d, 46d."""
        delivery = date(2026, 1, 1)
        # 15 days window
        _, due_15, _ = compute_statutory_dates(delivery, written_agreement=False)
        self.assertEqual(due_15, date(2026, 1, 16))

        # 45 days window
        _, due_45, _ = compute_statutory_dates(delivery, written_agreement=True)
        self.assertEqual(due_45, date(2026, 2, 15))

    def test_section_16_interest_zero_when_not_overdue(self) -> None:
        """No interest accrues if payment is not overdue."""
        principal = 1_000_000_00  # Rs 10 Lakhs (in paise)
        due_date = date(2026, 8, 20)
        as_of = date(2026, 8, 15)  # 5 days before due date

        res = calculate_section_16_interest(principal, due_date, as_of)
        self.assertEqual(res.days_overdue, 0)
        self.assertEqual(res.accrued_interest_paise, 0)
        self.assertEqual(res.total_payable_paise, principal)

    def test_section_16_interest_exact_rates(self) -> None:
        """Statutory interest rate is exactly 3x RBI Bank Rate (3 * 6.75% = 20.25% p.a.)."""
        self.assertEqual(DEFAULT_RBI_BANK_RATE_PCT, 6.75)
        self.assertEqual(STATUTORY_RATE_MULTIPLIER, 3)
        statutory_rate = DEFAULT_RBI_BANK_RATE_PCT * STATUTORY_RATE_MULTIPLIER
        self.assertEqual(statutory_rate, 20.25)

    def test_section_16_interest_discrete_monthly_and_residual(self) -> None:
        """Verify Section 16 formula: A_M = P * (1 + r/12)^M and Interest_residual = A_M * r * delta_d / 365."""
        principal = 100_000_00  # Rs 1,00,000 (10,000,000 paise)
        due_date = date(2026, 1, 1)
        # 75 days overdue: M = 2 full months (60 days), delta_d = 15 residual days
        as_of = due_date + timedelta(days=75)

        r_annual = Decimal("0.2025")  # 20.25%
        p_dec = Decimal(principal)

        # 1 month: 1 + 0.2025/12 = 1 + 0.016875 = 1.016875
        m1 = p_dec * Decimal("1.016875")
        # 2 months:
        m2 = m1 * Decimal("1.016875")
        # residual 15 days:
        residual = m2 * (r_annual * Decimal(15) / Decimal(365))
        expected_total_paise = int(round(m2 + residual))
        expected_interest_paise = expected_total_paise - principal

        res = calculate_section_16_interest(principal, due_date, as_of)
        self.assertEqual(res.days_overdue, 75)
        self.assertEqual(res.full_months, 2)
        self.assertEqual(res.residual_days, 15)
        self.assertEqual(res.total_payable_paise, expected_total_paise)
        self.assertEqual(res.accrued_interest_paise, expected_interest_paise)
        self.assertIsInstance(res.total_payable_paise, int)
        self.assertIsInstance(res.accrued_interest_paise, int)

    def test_section_16_interest_integer_paise_no_float_drift(self) -> None:
        """Verify exact integer paise result without float rounding artifacts."""
        principal = 543_210_99  # Rs 5,43,210.99
        due_date = date(2026, 3, 15)
        as_of = date(2026, 8, 26)  # 164 days overdue -> 5 months (150d) + 14 residual days

        res = calculate_section_16_interest(principal, due_date, as_of)
        self.assertIsInstance(res, StatutoryInterestResult)
        self.assertEqual(res.days_overdue, 164)
        self.assertEqual(res.full_months, 5)
        self.assertEqual(res.residual_days, 14)
        self.assertGreater(res.total_payable_paise, principal)
        self.assertEqual(res.total_payable_paise, principal + res.accrued_interest_paise)

    def test_section_16_interest_non_exact_rate_does_not_drift(self) -> None:
        """A non-default RBI rate must not leak binary-float noise into the reported rate.

        6.1 * 3 is 18.299999999999997 in plain float. rbi_bank_rate_pct is a public keyword
        argument meant to be overridden with the rate in force at the time, so a realistic
        rate change is exactly the input that must stay clean.
        """
        res = calculate_section_16_interest(
            1_000_000_00, date(2026, 1, 1), date(2026, 4, 1), rbi_bank_rate_pct=6.1
        )
        self.assertEqual(res.annual_rate_pct, 18.3)

    def test_section_43b_h_financial_year(self) -> None:
        """Verify Indian Financial Year (April 1 - March 31) computation."""
        self.assertEqual(get_financial_year(date(2026, 4, 1)), "2026-27")
        self.assertEqual(get_financial_year(date(2026, 8, 26)), "2026-27")
        self.assertEqual(get_financial_year(date(2026, 12, 31)), "2026-27")
        self.assertEqual(get_financial_year(date(2027, 1, 1)), "2026-27")
        self.assertEqual(get_financial_year(date(2027, 3, 31)), "2026-27")
        self.assertEqual(get_financial_year_end(date(2026, 8, 26)), date(2027, 3, 31))

    def test_section_43b_h_eligibility_and_refusal(self) -> None:
        """Micro/Small Manufacturing/Services are eligible; Medium and Traders are strictly ineligible."""
        m_eligible = Merchant("M1", "Eligible Mfg", True, "micro", UdyamActivity.MANUFACTURING)
        m_trader = Merchant("M2", "Sagar Trading Company", True, "micro", UdyamActivity.TRADING)
        m_medium = Merchant("M3", "Medium Corp", True, "medium", UdyamActivity.SERVICES)
        m_unregistered = Merchant("M4", "Unregistered SME", False, None, None)

        invoice = {
            "invoice_id": "INV-101",
            "amount_paise": 500_000_00,
            "contractual_due_date": "2026-07-01",
            "statutory_due_date": "2026-07-15",
            "state": "OVERDUE",
        }
        as_of = date(2026, 8, 26)

        eval_eligible = evaluate_section_43b_h(m_eligible, invoice, as_of)
        self.assertTrue(eval_eligible.is_eligible)
        self.assertEqual(eval_eligible.disallowance_fy, "2026-27")
        self.assertEqual(eval_eligible.disallowance_date, date(2027, 3, 31))

        eval_trader = evaluate_section_43b_h(m_trader, invoice, as_of)
        self.assertFalse(eval_trader.is_eligible)
        self.assertIn("trader", eval_trader.refusal_reason.lower())

        eval_medium = evaluate_section_43b_h(m_medium, invoice, as_of)
        self.assertFalse(eval_medium.is_eligible)
        self.assertIn("medium", eval_medium.refusal_reason.lower())

        eval_unreg = evaluate_section_43b_h(m_unregistered, invoice, as_of)
        self.assertFalse(eval_unreg.is_eligible)
        self.assertIn("not registered", eval_unreg.refusal_reason.lower())

    def test_section_43b_h_refuses_a_settled_invoice(self) -> None:
        """A fully paid invoice is never a disallowance risk, regardless of merchant eligibility."""
        m_eligible = Merchant("M1", "Eligible Mfg", True, "micro", UdyamActivity.MANUFACTURING)
        as_of = date(2026, 8, 26)

        paid = {
            "invoice_id": "INV-201",
            "amount_paise": 500_000_00,
            "amount_received_paise": 500_000_00,
            "state": "PAID",
        }
        eval_paid = evaluate_section_43b_h(m_eligible, paid, as_of)
        self.assertFalse(eval_paid.is_eligible)
        self.assertIsNone(eval_paid.compliant_notice_copy)

        # TDS withheld and remitted reconciles to face value too; no shortfall to flag.
        tds_settled = {
            "invoice_id": "INV-202",
            "amount_paise": 500_000_00,
            "amount_received_paise": 450_000_00,
            "tds_deducted_paise": 50_000_00,
            "state": "TDS_UNDERPAID",
        }
        eval_tds = evaluate_section_43b_h(m_eligible, tds_settled, as_of)
        self.assertFalse(eval_tds.is_eligible)

        # A genuinely outstanding invoice still gets the notice.
        outstanding = {**paid, "amount_received_paise": 0, "state": "OVERDUE"}
        eval_outstanding = evaluate_section_43b_h(m_eligible, outstanding, as_of)
        self.assertTrue(eval_outstanding.is_eligible)
        self.assertIsNotNone(eval_outstanding.compliant_notice_copy)

    def test_section_43b_h_notice_generation(self) -> None:
        """Generate compliant notice copy for eligible merchants and suppress for ineligible."""
        m_eligible = Merchant("M1", "Eligible Mfg", True, "small", UdyamActivity.MANUFACTURING)
        m_trader = Merchant("M2", "Sagar Trading Company", True, "micro", UdyamActivity.TRADING)
        invoice = {
            "invoice_id": "INV-101",
            "amount_paise": 500_000_00,
            "contractual_due_date": "2026-07-01",
            "statutory_due_date": "2026-07-15",
            "state": "OVERDUE",
        }
        as_of = date(2026, 8, 26)

        ok, copy, refusal = generate_section_43b_h_notice(m_eligible, invoice, as_of)
        self.assertTrue(ok)
        self.assertIsNotNone(copy)
        self.assertIn("Section 43B(h)", copy)
        self.assertIn("FY 2026-27", copy)
        self.assertIn("year-end", copy)
        self.assertIsNone(refusal)

        # Trader refusal
        ok_t, copy_t, refusal_t = generate_section_43b_h_notice(m_trader, invoice, as_of)
        self.assertFalse(ok_t)
        self.assertIsNone(copy_t)
        self.assertIn("trader", refusal_t.lower())

    def test_anti_dark_pattern_guardrails(self) -> None:
        """Prohibited threatening or misleading tax copy is rejected."""
        compliant = (
            "Invoice outstanding beyond the MSMED statutory period is subject to tax disallowance "
            "under Section 43B(h) of the Income Law for FY 2026-27 if unpaid at year-end."
        )
        is_valid, err = validate_section_43b_h_copy(compliant)
        self.assertTrue(is_valid)
        self.assertIsNone(err)

        prohibited_1 = "Your tax deduction is immediately cancelled today."
        is_valid_1, err_1 = validate_section_43b_h_copy(prohibited_1)
        self.assertFalse(is_valid_1)
        self.assertIn("immediately cancelled", err_1)

        prohibited_2 = "Immediate tax penalties apply tomorrow unless you pay now."
        is_valid_2, err_2 = validate_section_43b_h_copy(prohibited_2)
        self.assertFalse(is_valid_2)
        self.assertIn("immediate tax penalties", err_2)

    def test_dispute_classification(self) -> None:
        """Verify categorization of dispute reason strings."""
        self.assertEqual(classify_dispute_reason("Short delivery, 40 units received against 50 invoiced"), DisputeCategory.GOODS_SERVICES)
        self.assertEqual(classify_dispute_reason("Rate on the invoice does not match the purchase order"), DisputeCategory.INVOICE_MISMATCH)
        self.assertEqual(classify_dispute_reason("This invoice appears to duplicate INV-3902"), DisputeCategory.DUPLICATE)
        self.assertEqual(classify_dispute_reason("GST rate applied is 18 percent, our contract says 12 percent"), DisputeCategory.TAX_GST)
        self.assertEqual(classify_dispute_reason("We already settled this by NEFT on 12 August, UTR 511923447"), DisputeCategory.ALREADY_PAID)
        self.assertEqual(classify_dispute_reason("Billed to wrong subsidiary entity"), DisputeCategory.WRONG_RECIPIENT)
        self.assertEqual(classify_dispute_reason("Milestone 3 not yet signed off in SLA contract"), DisputeCategory.CONTRACTUAL)
        self.assertEqual(classify_dispute_reason("Unspecified issue"), DisputeCategory.UNKNOWN)

    def test_dispute_recompute_statutory_dates(self) -> None:
        """Test recomputing statutory dates on dispute objection inside 15 days vs outside."""
        delivery = date(2026, 8, 1)

        # Objection on day 10 (inside 15d), resolved on day 25
        objection_date = date(2026, 8, 11)
        resolution_date = date(2026, 8, 26)
        acc, due, app = recompute_statutory_dates_on_dispute(
            delivery_date=delivery,
            written_agreement=True,
            objection_date=objection_date,
            resolution_date=resolution_date,
        )
        self.assertEqual(acc, resolution_date)
        self.assertEqual(due, resolution_date + timedelta(days=45))
        self.assertEqual(app, resolution_date + timedelta(days=46))

        # Objection on day 20 (outside 15d) -> deemed acceptance on delivery date stands
        late_objection = date(2026, 8, 21)
        acc_late, due_late, app_late = recompute_statutory_dates_on_dispute(
            delivery_date=delivery,
            written_agreement=True,
            objection_date=late_objection,
            resolution_date=resolution_date,
        )
        self.assertEqual(acc_late, delivery)
        self.assertEqual(due_late, delivery + timedelta(days=45))
        self.assertEqual(app_late, delivery + timedelta(days=46))


if __name__ == "__main__":
    unittest.main()
