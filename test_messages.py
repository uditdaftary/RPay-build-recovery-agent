import unittest
from datetime import date

from app.envelope import Channel, Language, Strategy, Tone
from app.messages import draft_message_for_decision, validate_and_sanitize_copy
from app.strategist import StrategistDecision


class TestMessageDrafting(unittest.TestCase):
    def setUp(self):
        self.eligible_merchant = {
            "merchant_id": "MER-001",
            "name": "Apex Industrial Supplies",
            "udyam_registered": True,
            "udyam_category": "Small",
            "udyam_activity": "Manufacturing",
        }
        self.ineligible_trader = {
            "merchant_id": "MER-002",
            "name": "Sagar Trading Company",
            "udyam_registered": True,
            "udyam_category": "Micro",
            "udyam_activity": "Trading",
        }
        self.debtor = {
            "debtor_id": "DEB-001",
            "name": "Acme Builders",
            "preferred_channel": "email",
            "language": "en",
        }
        self.invoices = [{
            "invoice_id": "INV-101",
            "amount_paise": 500000_00,
            "amount_received_paise": 0,
            "tds_deducted_paise": 0,
            "days_overdue": 46,
            "contractual_due_date": "2026-07-10",
            "delivery_date": "2026-05-25",
            "written_agreement": True,
            "state": "OVERDUE",
        }]

    def test_eligible_merchant_statutory_notice_generation(self):
        decision = StrategistDecision(
            debtor_id="DEB-001",
            debtor_name="Acme Builders",
            strategy=Strategy.ESCALATE,
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            ask_amount_paise=500000_00,
            reasoning="Statutory escalation for overdue debt",
            confidence=0.95,
            resolution_url="http://localhost:8000/r/INV-101",
        )
        msg = draft_message_for_decision(decision, self.debtor, self.invoices, self.eligible_merchant, as_of=date(2026, 8, 26))
        self.assertTrue(msg.is_statutory)
        self.assertIn("Section 43B(h)", msg.body)
        self.assertIn("Section 16", msg.body)
        self.assertIn("http://localhost:8000/r/INV-101", msg.body)
        self.assertTrue(msg.dark_pattern_clean)

    def test_trader_refusal_beat_strictly_suppresses_statutory_threats(self):
        decision = StrategistDecision(
            debtor_id="DEB-001",
            debtor_name="Acme Builders",
            strategy=Strategy.REQUEST_PAYMENT,
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FIRM,
            ask_amount_paise=500000_00,
            reasoning="Commercial collection; supplier ineligible for MSMED notices",
            confidence=0.90,
            resolution_url="http://localhost:8000/r/INV-101",
        )
        msg = draft_message_for_decision(decision, self.debtor, self.invoices, self.ineligible_trader, as_of=date(2026, 8, 26))
        self.assertFalse(msg.is_statutory)
        self.assertNotIn("43B(h)", msg.body)
        self.assertNotIn("MSMED", msg.body)
        self.assertNotIn("penal interest", msg.body.lower())
        self.assertIn("payment", msg.body.lower())

    def test_hinglish_message_drafting(self):
        debtor_hi = dict(self.debtor, language="hinglish")
        decision = StrategistDecision(
            debtor_id="DEB-001",
            debtor_name="Acme Builders",
            strategy=Strategy.REQUEST_PAYMENT,
            channel=Channel.WHATSAPP,
            language=Language.HINGLISH,
            tone=Tone.COLLABORATIVE,
            ask_amount_paise=500000_00,
            reasoning="Hinglish WhatsApp reminder",
            confidence=0.92,
            resolution_url="http://localhost:8000/r/INV-101",
        )
        msg = draft_message_for_decision(decision, debtor_hi, self.invoices, self.eligible_merchant, as_of=date(2026, 8, 26))
        self.assertEqual(msg.language, Language.HINGLISH)
        self.assertTrue(any(k in msg.body.lower() for k in ["namaste", "invoice", "clear", "kripya", "payment"]))

    def test_tds_reconciliation_message_drafting(self):
        tds_invoices = [{
            "invoice_id": "INV-102",
            "amount_paise": 1000000_00,
            "amount_received_paise": 980000_00,
            "tds_deducted_paise": 20000_00,
            "days_overdue": 30,
            "contractual_due_date": "2026-07-26",
            "state": "TDS_UNDERPAID",
        }]
        decision = StrategistDecision(
            debtor_id="DEB-001",
            debtor_name="Acme Builders",
            strategy=Strategy.RECONCILE,
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.COLLABORATIVE,
            ask_amount_paise=0,
            reasoning="Reconcile TDS Form 26AS certificate",
            confidence=0.95,
            resolution_url="http://localhost:8000/r/INV-102",
        )
        msg = draft_message_for_decision(decision, self.debtor, tds_invoices, self.eligible_merchant, as_of=date(2026, 8, 26))
        self.assertIn("Form 26AS", msg.body)
        self.assertNotIn("underpaid", msg.body.lower())
        self.assertNotIn("overdue penalty", msg.body.lower())

    def test_anti_dark_pattern_rejection_and_sanitization(self):
        dirty_copy = "Your tax deduction is immediately cancelled today and penalties apply tomorrow!"
        is_clean, reason = validate_and_sanitize_copy(dirty_copy)
        self.assertFalse(is_clean)
        self.assertIn("Prohibited", reason)

if __name__ == "__main__":
    unittest.main()
