import os
import time
import unittest
from unittest.mock import patch

from app.channels import OUTBOX_DIR, dispatch_email, dispatch_message, dispatch_whatsapp
from app.envelope import Channel, Language, Tone
from app.messages import DraftedMessage


class TestChannels(unittest.TestCase):
    def test_mock_email_dispatch_creates_outbox_file_and_audit(self):
        msg = DraftedMessage(
            debtor_id="DEB-001",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Payment Reminder - INV-101",
            body="Please clear your pending balance.",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        res = dispatch_message(msg, recipient_email="accounts@acme.com", recipient_phone=None)
        self.assertTrue(res.success)
        self.assertEqual(res.channel, Channel.EMAIL)
        self.assertTrue(res.simulated)
        self.assertTrue(res.message_id.startswith("msg_"))
        outbox_file = OUTBOX_DIR / f"{res.message_id}.txt"
        self.assertTrue(outbox_file.exists())
        self.assertIn("accounts@acme.com", outbox_file.read_text(encoding="utf-8"))

    def test_whatsapp_interactive_payload_formatting(self):
        msg = DraftedMessage(
            debtor_id="DEB-002",
            channel=Channel.WHATSAPP,
            language=Language.HINGLISH,
            tone=Tone.COLLABORATIVE,
            subject="",
            body="Namaste, INV-102 due hai. Link: http://localhost:8000/r/INV-102",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        res = dispatch_message(msg, recipient_email=None, recipient_phone="+919876543210")
        self.assertTrue(res.success)
        self.assertEqual(res.channel, Channel.WHATSAPP)
        self.assertTrue(res.simulated)
        self.assertTrue(res.message_id.startswith("wa_"))
        outbox_file = OUTBOX_DIR / f"{res.message_id}.json"
        self.assertTrue(outbox_file.exists())

    def test_no_contact_channel_dispatch(self):
        msg = DraftedMessage(
            debtor_id="DEB-003",
            channel=Channel.NONE,
            language=Language.EN,
            tone=Tone.NEUTRAL,
            subject="Hold",
            body="Restraint",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        res = dispatch_message(msg)
        self.assertTrue(res.success)
        self.assertEqual(res.channel, Channel.NONE)
        self.assertTrue(res.simulated)

    def test_portal_channel_dispatch(self):
        msg = DraftedMessage(
            debtor_id="DEB-004",
            channel=Channel.PORTAL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Portal Notification - INV-104",
            body="Please check your vendor portal notification.",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        res = dispatch_message(msg)
        self.assertTrue(res.success)
        self.assertEqual(res.channel, Channel.PORTAL)
        self.assertTrue(res.simulated)
        self.assertTrue(res.message_id.startswith("portal_DEB-004_"))
        self.assertIsNotNone(res.payload)
        self.assertEqual(res.payload["subject"], "Portal Notification - INV-104")

    def test_resend_error_propagation_on_live_dispatch_failure(self):
        msg = DraftedMessage(
            debtor_id="DEB-005",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Payment Reminder",
            body="Overdue balance reminder.",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        with (
            patch.dict(os.environ, {"RESEND_API_KEY": "re_test_12345"}),
            patch("urllib.request.urlopen", side_effect=Exception("API connection timed out")),
        ):
            res = dispatch_email(msg, "buyer@domain.in", dry_run=False)
            self.assertTrue(res.success)
            self.assertTrue(res.simulated)
            self.assertIsNotNone(res.error)
            self.assertIn("API connection timed out", res.error)

    def test_unique_millisecond_outbox_filenames(self):
        msg1 = DraftedMessage(
            debtor_id="DEB-006",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Notice 1",
            body="First notice",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        msg2 = DraftedMessage(
            debtor_id="DEB-006",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Notice 2",
            body="Second notice",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        res1 = dispatch_email(msg1, "accounts@acme.com")
        time.sleep(0.005)
        res2 = dispatch_email(msg2, "accounts@acme.com")
        self.assertNotEqual(res1.message_id, res2.message_id)
        file1 = OUTBOX_DIR / f"{res1.message_id}.txt"
        file2 = OUTBOX_DIR / f"{res2.message_id}.txt"
        self.assertTrue(file1.exists())
        self.assertTrue(file2.exists())
        self.assertIn("First notice", file1.read_text(encoding="utf-8"))
        self.assertIn("Second notice", file2.read_text(encoding="utf-8"))

    def test_whatsapp_dry_run_flag_in_payload_and_audit(self):
        msg = DraftedMessage(
            debtor_id="DEB-007",
            channel=Channel.WHATSAPP,
            language=Language.EN,
            tone=Tone.COLLABORATIVE,
            subject="",
            body="WhatsApp message",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        res = dispatch_whatsapp(msg, "+919876543210", dry_run=True)
        self.assertTrue(res.success)
        self.assertTrue(res.payload.get("dry_run"))

    def test_drafted_message_embedded_contact_fallback(self):
        msg = DraftedMessage(
            debtor_id="DEB-008",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Reminder",
            body="Body text",
            is_statutory=False,
            dark_pattern_clean=True,
            recipient_email="embedded@custom.in",
        )
        res = dispatch_message(msg)
        self.assertTrue(res.success)
        self.assertEqual(res.payload.get("to"), "embedded@custom.in")


if __name__ == "__main__":
    unittest.main()
