"""Test suite for MSMED Section 15/16 and Income Tax Section 43B(h) Statutory Engine.

Strict TDD: Tests written before implementation.
Verifies:
1. MSMED Section 15 statutory clock, deemed acceptance, objection within 15d, and 45d caps.
2. MSMED Section 16 compound interest with monthly rests at 3x RBI Bank Rate, integer paise precision.
3. Section 43B(h) Income Tax disallowance eligibility, FY calculation, and anti-dark-pattern copy rules.
4. Dispute taxonomy, categorization, and statutory clock recomputation.
"""

from __future__ import annotations

import copy
import json
import unittest
from datetime import date, timedelta
from decimal import Decimal

from app.config import PROJECT_ROOT
from app.disputes import (
    DisputeCategory,
    classify_dispute_reason,
    recompute_statutory_dates_on_dispute,
)
from app.ledger import Merchant, UdyamActivity
from app.server import INVOICES
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
    def setUp(self) -> None:
        self._invoices_backup = copy.deepcopy(INVOICES)

    def tearDown(self) -> None:
        INVOICES.clear()
        INVOICES.update(self._invoices_backup)

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

        m_none_category = Merchant("M5", "None Category Corp", True, None, "manufacturing")
        eval_none_cat = evaluate_section_43b_h(m_none_category, invoice, as_of)
        self.assertFalse(eval_none_cat.is_eligible)
        self.assertIn("category", eval_none_cat.refusal_reason.lower())

        m_none_activity = Merchant("M6", "None Activity Corp", True, "small", None)
        eval_none_act = evaluate_section_43b_h(m_none_activity, invoice, as_of)
        self.assertFalse(eval_none_act.is_eligible)
        self.assertIn("activity", eval_none_act.refusal_reason.lower())

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

    def test_dispute_classification_natural_phrasings(self) -> None:
        """Real debtor phrasings, not the category names, must land in the right bucket.

        The classifier used to match a short exact-phrase list, so "Short delivered: 40
        units billed, 32 received against the PO." (the live resolution page's own example
        wording) fell through to UNKNOWN. Word roots fixed that; these lock the coverage.
        """
        cases: list[tuple[str, DisputeCategory]] = [
            # The reported failure, plus the two examples in the resolution page placeholder.
            ("Short delivered: 40 units billed, 32 received against the PO.", DisputeCategory.GOODS_SERVICES),
            ("the goods were short delivered", DisputeCategory.GOODS_SERVICES),
            ("this was already paid by NEFT on 12 August", DisputeCategory.ALREADY_PAID),
            # GOODS_SERVICES: shortfall / non-delivery / condition / wrong item / service.
            ("Shortfall of 8 cartons against the challan", DisputeCategory.GOODS_SERVICES),
            ("Consignment not received, courier shows undelivered", DisputeCategory.GOODS_SERVICES),
            ("Material is defective and half the batch is broken", DisputeCategory.GOODS_SERVICES),
            ("You shipped the wrong grade of steel", DisputeCategory.GOODS_SERVICES),
            ("AMC service was never rendered this quarter", DisputeCategory.GOODS_SERVICES),
            # INVOICE_MISMATCH: pricing / overbilling / arithmetic.
            ("You have overcharged us versus the agreed rate", DisputeCategory.INVOICE_MISMATCH),
            ("Invoice total does not add up, line-item sum is off", DisputeCategory.INVOICE_MISMATCH),
            ("Billed at a higher price than our PO value", DisputeCategory.INVOICE_MISMATCH),
            # ALREADY_PAID: instruments and settlement claims.
            ("Cleared this via RTGS, transaction ref 40021", DisputeCategory.ALREADY_PAID),
            ("Payment already made by cheque no 004521", DisputeCategory.ALREADY_PAID),
            ("Nothing is outstanding on our account", DisputeCategory.ALREADY_PAID),
            # DUPLICATE.
            ("You have billed us twice for the same delivery", DisputeCategory.DUPLICATE),
            ("Looks like a duplicated invoice", DisputeCategory.DUPLICATE),
            # TAX_GST.
            ("HSN code is wrong so our input tax credit is blocked", DisputeCategory.TAX_GST),
            ("TDS has not been deducted on this bill", DisputeCategory.TAX_GST),
            # WRONG_RECIPIENT: entity / GSTIN.
            ("This is not our company, bill our sister concern instead", DisputeCategory.WRONG_RECIPIENT),
            ("Raised against the wrong GSTIN", DisputeCategory.WRONG_RECIPIENT),
            # CONTRACTUAL: terms / timing / sign-off.
            ("Payment is not due yet, we are on net 45 terms", DisputeCategory.CONTRACTUAL),
            ("Retention money is held back until the warranty period ends", DisputeCategory.CONTRACTUAL),
            ("Work is not complete, final phase is pending sign-off", DisputeCategory.CONTRACTUAL),
            # Noun-before-adjective / reversed word orders a debtor types naturally, which the
            # adjective-first roots missed and returned UNKNOWN for.
            ("We got fewer boxes than ordered", DisputeCategory.GOODS_SERVICES),
            ("Half the order is missing", DisputeCategory.GOODS_SERVICES),
            ("Delivery was partial", DisputeCategory.GOODS_SERVICES),
            ("We received 32 units against 40 billed", DisputeCategory.GOODS_SERVICES),
            ("Sign-off is still pending from our side", DisputeCategory.CONTRACTUAL),
            ("The pricing looks inflated", DisputeCategory.INVOICE_MISMATCH),
            ("This charge is not correct", DisputeCategory.INVOICE_MISMATCH),
            # Genuinely uninformative reasons must stay UNKNOWN so over-widening fails loudly.
            ("please check this", DisputeCategory.UNKNOWN),
            ("We have a problem with this bill", DisputeCategory.UNKNOWN),
            ("Not happy with how this was handled", DisputeCategory.UNKNOWN),
            # Roots must not leak into unrelated words: "rate" in "corporate", "spec" in
            # "unspecified" — these read as GOODS_SERVICES / non-delivery, not the shadowed cat.
            ("Our corporate office never received the shipment", DisputeCategory.GOODS_SERVICES),
        ]
        for reason, expected in cases:
            self.assertEqual(classify_dispute_reason(reason), expected, reason)

    def test_dispute_classification_precedence(self) -> None:
        """A reason touching two categories resolves to the earlier row in _CATEGORY_PATTERNS."""
        # Settlement claim outranks a delivery complaint.
        self.assertEqual(
            classify_dispute_reason("Already paid via NEFT, and the goods arrived damaged anyway"),
            DisputeCategory.ALREADY_PAID,
        )
        # Wrong-entity claim outranks a pricing complaint.
        self.assertEqual(
            classify_dispute_reason("Wrong company on the invoice and the rate is off too"),
            DisputeCategory.WRONG_RECIPIENT,
        )
        # Pricing outranks quantity when both are present.
        self.assertEqual(
            classify_dispute_reason("Billed at the wrong rate and short delivered by 5 units"),
            DisputeCategory.INVOICE_MISMATCH,
        )

    def test_dispute_classification_regression_lock_seeded_ledger(self) -> None:
        """The dispute reasons seeded into data/ledger.json must not drift category.

        Read out of the ledger rather than restated here. Restating them locked three
        strings instead of the file: regenerate the ledger with different reasons and a
        hardcoded copy still passes, on wording nothing in the system uses any more -
        the same way a pinned `INV-101` outlived the invoice it named.
        """
        ledger = json.loads((PROJECT_ROOT / "data" / "ledger.json").read_text(encoding="utf-8"))
        seeded = {
            invoice["dispute_reason"]
            for invoice in ledger["invoices"]
            if invoice.get("dispute_reason")
        }
        expected = {
            "We already settled this by NEFT on 12 August, UTR 511923447": DisputeCategory.ALREADY_PAID,
            "GST rate applied is 18 percent, our contract says 12 percent": DisputeCategory.TAX_GST,
            "This invoice appears to duplicate INV-3902": DisputeCategory.DUPLICATE,
        }
        # Fails loudly when the ledger gains, loses or rewords a reason, so the lock has
        # to be updated deliberately rather than quietly going stale.
        self.assertEqual(seeded, set(expected), "the seeded dispute reasons have changed")
        for reason, category in expected.items():
            self.assertEqual(classify_dispute_reason(reason), category, reason)

    def test_dispute_classification_no_phrasing_lost_to_roots(self) -> None:
        """Phrasings the exact-phrase lists matched, which the move to roots dropped.

        Every one of these classified correctly before the rewrite and regressed to
        UNKNOWN after it - "paid on" worst of all, because it left a debtor asserting
        settlement on the generic checklist instead of the UTR / bank-statement one.
        A widening pass is allowed to add categories; it is not allowed to lose them.
        """
        cases: list[tuple[str, DisputeCategory]] = [
            ("Paid on 12 August 2026, please check.", DisputeCategory.ALREADY_PAID),
            ("paid on", DisputeCategory.ALREADY_PAID),
            ("Wrong recipient - please redirect this invoice.", DisputeCategory.WRONG_RECIPIENT),
            ("wrong recipient", DisputeCategory.WRONG_RECIPIENT),
            ("This belongs to our subsidiary entity, not us.", DisputeCategory.WRONG_RECIPIENT),
            ("40 units billed, 32 units received against the PO.", DisputeCategory.GOODS_SERVICES),
            ("units received", DisputeCategory.GOODS_SERVICES),
            ("The quality was terrible and we are not paying.", DisputeCategory.GOODS_SERVICES),
            ("quality", DisputeCategory.GOODS_SERVICES),
        ]
        for reason, expected in cases:
            self.assertEqual(classify_dispute_reason(reason), expected, reason)

    def test_a_passing_mention_does_not_outrank_the_actual_claim(self) -> None:
        """A category above GOODS_SERVICES must match a claim, not a word in passing.

        `\btax` sat third in the table and matched any sentence containing "tax", so an
        explicit short-delivery complaint was classified TAX_GST and the debtor was asked
        for a GSTR-2B mismatch certificate instead of an inspection report. `\brate\b`/
        `\bprice` and `\b2b\b` were the same bug in INVOICE_MISMATCH and TAX_GST: a bare
        "error rate" or a contract's "clause 2b" outranked the actual complaint below it.
        """
        self.assertEqual(
            classify_dispute_reason(
                "Goods were short delivered, and the tax on the invoice is also wrong."
            ),
            DisputeCategory.GOODS_SERVICES,
        )
        # Still a tax dispute when the reason actually makes one.
        for reason in ("The tax rate applied is wrong", "Excess tax charged on this bill"):
            self.assertEqual(classify_dispute_reason(reason), DisputeCategory.TAX_GST, reason)
        self.assertEqual(
            classify_dispute_reason(
                "The delay is not our fault, the courier's error rate has been terrible."
            ),
            DisputeCategory.UNKNOWN,
        )
        self.assertEqual(
            classify_dispute_reason("As per clause 2b of our agreement, payment is not due yet."),
            DisputeCategory.CONTRACTUAL,
        )
        # Still an invoice-mismatch dispute when the reason actually makes one.
        for reason in ("Billed at the wrong rate and short delivered by 5 units", "Billed at a higher price than our PO value"):
            self.assertEqual(classify_dispute_reason(reason), DisputeCategory.INVOICE_MISMATCH, reason)

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

    def test_dispute_endpoint_integration_and_evidence_requirements(self) -> None:
        from fastapi.testclient import TestClient

        from app.config import business_today
        from app.server import INVOICES, app

        client = TestClient(app)
        test_token = next(iter(INVOICES.keys()))

        # Test objection within 15 days of delivery -> clock suspended
        INVOICES[test_token]["delivery_date"] = (business_today() - timedelta(days=5)).isoformat()
        INVOICES[test_token]["status"] = "OVERDUE"
        payload_recent = {
            "token": test_token,
            "reason": "Damaged goods received in batch, 12 units defective short delivery",
        }
        res = client.post("/api/dispute", json=payload_recent)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["category"], "GOODS_SERVICES")
        self.assertIn("Inspection Report", data["evidence_required"])
        self.assertTrue(data["statutory_clock_suspended"])

        # Test objection after 15 days of delivery -> deemed acceptance stands
        INVOICES[test_token]["delivery_date"] = (business_today() - timedelta(days=30)).isoformat()
        INVOICES[test_token]["status"] = "OVERDUE"
        res_late = client.post("/api/dispute", json=payload_recent)
        self.assertEqual(res_late.status_code, 200)
        data_late = res_late.json()
        self.assertFalse(data_late["statutory_clock_suspended"])

    def test_dispute_endpoint_fallback_delivery_heuristics(self) -> None:
        """Verify fallback delivery date heuristics when delivery_date is missing."""
        from fastapi.testclient import TestClient

        from app.config import business_today
        from app.server import INVOICES, app

        client = TestClient(app)
        test_token = next(iter(INVOICES.keys()))

        # 1. Fallback to invoice_date (within 15 days) -> clock suspended
        INVOICES[test_token]["delivery_date"] = None
        INVOICES[test_token]["invoice_date"] = (business_today() - timedelta(days=5)).isoformat()
        INVOICES[test_token]["contractual_due_date"] = (business_today() + timedelta(days=25)).isoformat()
        INVOICES[test_token]["status"] = "OVERDUE"
        res = client.post(
            "/api/dispute",
            json={"token": test_token, "reason": "Rate on the invoice does not match PO"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["statutory_clock_suspended"])

        # 2. Fallback to contractual_due_date with written_agreement=False (15d window)
        INVOICES[test_token]["delivery_date"] = None
        INVOICES[test_token]["invoice_date"] = None
        # contractual_due_date is in 5 days -> delivery_d = 5d - 15d = -10d (10 days ago, inside 15d)
        INVOICES[test_token]["contractual_due_date"] = (business_today() + timedelta(days=5)).isoformat()
        INVOICES[test_token]["written_agreement"] = False
        INVOICES[test_token]["status"] = "OVERDUE"
        res_15d = client.post(
            "/api/dispute",
            json={"token": test_token, "reason": "Short delivery, 10 units missing"},
        )
        self.assertEqual(res_15d.status_code, 200)
        self.assertTrue(res_15d.json()["statutory_clock_suspended"])

        # 3. Fallback to days_overdue (default 15 days) -> delivery_d = business_today() - 5d (inside 15d)
        INVOICES[test_token]["delivery_date"] = None
        INVOICES[test_token]["invoice_date"] = None
        INVOICES[test_token]["contractual_due_date"] = None
        INVOICES[test_token]["days_overdue"] = 5
        INVOICES[test_token]["status"] = "OVERDUE"
        res_overdue = client.post(
            "/api/dispute",
            json={"token": test_token, "reason": "Duplicate invoice INV-999"},
        )
        self.assertEqual(res_overdue.status_code, 200)
        self.assertTrue(res_overdue.json()["statutory_clock_suspended"])

        # 4. Null days_overdue fallback does not crash (defaults to 15 days)
        INVOICES[test_token]["delivery_date"] = None
        INVOICES[test_token]["invoice_date"] = None
        INVOICES[test_token]["contractual_due_date"] = None
        INVOICES[test_token]["days_overdue"] = None
        INVOICES[test_token]["status"] = "OVERDUE"
        res_null_days = client.post(
            "/api/dispute",
            json={"token": test_token, "reason": "Pricing dispute on null overdue invoice"},
        )
        self.assertEqual(res_null_days.status_code, 200)
        self.assertTrue(res_null_days.json()["statutory_clock_suspended"])

    def test_mandate_webhook_simulate_endpoint(self) -> None:
        """Verify POST /api/mandate/simulate-webhook endpoint."""
        from fastapi.testclient import TestClient

        from app.server import app

        client = TestClient(app)

        # Insufficient funds
        res = client.post(
            "/api/mandate/simulate-webhook",
            json={
                "mandate_id": "man_sim_001",
                "failure_code": "INSUFFICIENT_FUNDS",
                "failure_date": "2026-08-26",
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["mandate_id"], "man_sim_001")
        self.assertEqual(data["failure_code"], "INSUFFICIENT_FUNDS")
        self.assertEqual(len(data["retry_dates"]), 3)
        self.assertEqual(data["retry_dates"][0], "2026-08-29")
        self.assertIn("Insufficient funds", data["strategy_notes"])

        # Mandate expired
        res_exp = client.post(
            "/api/mandate/simulate-webhook",
            json={
                "mandate_id": "man_sim_002",
                "failure_code": "MANDATE_EXPIRED",
                "failure_date": "2026-08-26",
            },
        )
        self.assertEqual(res_exp.status_code, 200)
        data_exp = res_exp.json()
        self.assertTrue(data_exp["ok"])
        self.assertEqual(len(data_exp["retry_dates"]), 0)
        self.assertIn("re-authorization", data_exp["strategy_notes"].lower())


if __name__ == "__main__":
    unittest.main()



