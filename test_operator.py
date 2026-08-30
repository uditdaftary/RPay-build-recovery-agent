import unittest

from fastapi.testclient import TestClient

from app.envelope import Channel, Language, Tone
from app.messages import DraftedMessage
from app.operator import get_review_queue, is_kill_switch_active, queue_for_review, set_kill_switch
from app.server import app


class TestOperatorSurface(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        set_kill_switch(False)

    def test_kill_switch_toggle_and_state(self):
        res = self.client.post("/api/operator/kill-switch", json={"active": True})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["kill_switch_active"])
        self.assertTrue(is_kill_switch_active())

        # Reset
        res = self.client.post("/api/operator/kill-switch", json={"active": False})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["kill_switch_active"])
        self.assertFalse(is_kill_switch_active())

    def test_review_queue_approve_and_reject(self):
        msg = DraftedMessage(
            debtor_id="DEB-099",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Statutory Notice",
            body="Section 15 demand.",
            is_statutory=True,
            dark_pattern_clean=True,
        )
        queue_for_review(
            debtor_id="DEB-099",
            debtor_name="Special Review Corp",
            strategy="ESCALATE",
            ask_amount_paise=100000_00,
            reasoning="High exposure statutory notice",
            draft=msg,
        )
        queue = get_review_queue()
        self.assertTrue(any(item["debtor_id"] == "DEB-099" for item in queue))

        # Approve item
        res = self.client.post("/api/operator/approve", json={"debtor_id": "DEB-099"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["approved"])

    def test_operator_dashboard_renders(self):
        res = self.client.get("/operator")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Review-First Mode", res.text)
        self.assertIn("Kill Switch", res.text)

    def test_audit_log_export_json_and_csv(self):
        res_json = self.client.get("/api/operator/export?format=json")
        self.assertEqual(res_json.status_code, 200)
        self.assertEqual(res_json.headers["content-type"], "application/json")

        res_csv = self.client.get("/api/operator/export?format=csv")
        self.assertEqual(res_csv.status_code, 200)
        self.assertIn("text/csv", res_csv.headers["content-type"])

if __name__ == "__main__":
    unittest.main()
