import unittest

from app.channels import dispatch_message
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

if __name__ == "__main__":
    unittest.main()
