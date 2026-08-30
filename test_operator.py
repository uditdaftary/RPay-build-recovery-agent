import csv
import io
import unittest

from fastapi.testclient import TestClient

from app import audit
from app.envelope import Channel, Language, Tone
from app.messages import DraftedMessage
from app.operator import (
    _REVIEW_QUEUE,
    get_review_queue,
    is_kill_switch_active,
    queue_for_review,
    set_kill_switch,
)
from app.server import app


class TestOperatorSurface(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["X-Operator-Key"] = "operator-secret-key"
        set_kill_switch(False)
        _REVIEW_QUEUE.clear()

    def tearDown(self):
        set_kill_switch(False)
        _REVIEW_QUEUE.clear()

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
        self.assertEqual(len(get_review_queue()), 0)

    def test_review_queue_reject(self):
        msg = DraftedMessage(
            debtor_id="DEB-REJ-01",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Notice",
            body="Demand body.",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        queue_for_review(
            debtor_id="DEB-REJ-01",
            debtor_name="Reject Corp",
            strategy="ESCALATE",
            ask_amount_paise=50000_00,
            reasoning="Testing rejection path",
            draft=msg,
        )
        self.assertEqual(len(get_review_queue()), 1)

        # Reject item
        rejection_reason = "Manual operator commercial override"
        res = self.client.post(
            "/api/operator/reject",
            json={"debtor_id": "DEB-REJ-01", "reason": rejection_reason},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["rejected"])
        self.assertEqual(res.json()["debtor_id"], "DEB-REJ-01")
        self.assertEqual(res.json()["reason"], rejection_reason)
        self.assertEqual(len(get_review_queue()), 0)

        # Verify rejection logged to audit trail
        events = audit.read_all()
        rejection_events = [
            e for e in events
            if e.get("event") == "operator.review_rejected" and e.get("debtor_id") == "DEB-REJ-01"
        ]
        self.assertTrue(len(rejection_events) > 0)
        self.assertEqual(rejection_events[-1].get("reason"), rejection_reason)
        self.assertEqual(rejection_events[-1].get("operator_reason"), rejection_reason)

    def test_operator_endpoints_not_found(self):
        # 404 when debtor not in review queue
        res_approve = self.client.post(
            "/api/operator/approve", json={"debtor_id": "NON-EXISTENT"}
        )
        self.assertEqual(res_approve.status_code, 404)
        self.assertIn("error", res_approve.json())

        res_reject = self.client.post(
            "/api/operator/reject", json={"debtor_id": "NON-EXISTENT", "reason": "test"}
        )
        self.assertEqual(res_reject.status_code, 404)
        self.assertIn("error", res_reject.json())

    def test_approval_blocked_when_kill_switch_active(self):
        msg = DraftedMessage(
            debtor_id="DEB-KILL-01",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Urgent Notice",
            body="Demand body.",
            is_statutory=True,
            dark_pattern_clean=True,
        )
        queue_for_review(
            debtor_id="DEB-KILL-01",
            debtor_name="Kill Switch Test Corp",
            strategy="ESCALATE",
            ask_amount_paise=75000_00,
            reasoning="Checking kill switch safety under lock",
            draft=msg,
        )
        # Activate kill switch
        set_kill_switch(True)

        res = self.client.post("/api/operator/approve", json={"debtor_id": "DEB-KILL-01"})
        self.assertEqual(res.status_code, 409)
        self.assertFalse(res.json().get("approved", True))
        self.assertIn("kill switch", res.json().get("error", "").lower())

        # Crucial check: Item MUST NOT be lost from the queue
        queue = get_review_queue()
        self.assertTrue(any(item["debtor_id"] == "DEB-KILL-01" for item in queue))

        # Deactivate kill switch
        set_kill_switch(False)

        # Now approval should succeed
        res2 = self.client.post("/api/operator/approve", json={"debtor_id": "DEB-KILL-01"})
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.json().get("approved"))
        self.assertEqual(len(get_review_queue()), 0)

    def test_dynamic_csv_audit_export(self):
        # Trigger an action that creates custom audit fields
        msg = DraftedMessage(
            debtor_id="DEB-CSV-01",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Notice for CSV",
            body="Demand body for CSV.",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        queue_for_review(
            debtor_id="DEB-CSV-01",
            debtor_name="CSV Export Corp",
            strategy="ESCALATE",
            ask_amount_paise=30000_00,
            reasoning="Audit CSV test",
            draft=msg,
        )
        custom_reason = "Special compliance rejection reason"
        self.client.post(
            "/api/operator/reject",
            json={"debtor_id": "DEB-CSV-01", "reason": custom_reason},
        )

        res_csv = self.client.get("/api/operator/export?format=csv")
        self.assertEqual(res_csv.status_code, 200)
        self.assertIn("text/csv", res_csv.headers["content-type"])

        # Parse CSV
        reader = csv.reader(io.StringIO(res_csv.text))
        header = next(reader)
        # Standard keys appear first
        expected_standard = ["event", "ts", "debtor_id", "invoice_id", "strategy", "reason", "error"]
        self.assertEqual(header[:len(expected_standard)], expected_standard)
        # Custom keys like operator_reason appear dynamically
        self.assertIn("operator_reason", header)

        # Verify rejection row has both reason and operator_reason filled
        dict_reader = csv.DictReader(io.StringIO(res_csv.text))
        rejection_rows = [
            r for r in dict_reader
            if r.get("event") == "operator.review_rejected" and r.get("debtor_id") == "DEB-CSV-01"
        ]
        self.assertTrue(len(rejection_rows) > 0)
        self.assertEqual(rejection_rows[-1]["reason"], custom_reason)
        self.assertEqual(rejection_rows[-1]["operator_reason"], custom_reason)

    def test_operator_dashboard_renders(self):
        msg = DraftedMessage(
            debtor_id="DEB-DASH-01",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Dashboard Subject Line",
            body="Dashboard message body preview text.",
            is_statutory=True,
            dark_pattern_clean=True,
        )
        queue_for_review(
            debtor_id="DEB-DASH-01",
            debtor_name="Dashboard Debtor Ltd",
            strategy="ESCALATE",
            ask_amount_paise=99000_00,
            reasoning="Dashboard reasoning test preview",
            draft=msg,
        )
        res = self.client.get("/operator")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Review-First Mode", res.text)
        self.assertIn("Kill Switch", res.text)
        # Verify message details & reasoning rendered (BUG-022)
        self.assertIn("Dashboard reasoning test preview", res.text)
        self.assertIn("Dashboard Subject Line", res.text)
        self.assertIn("Dashboard message body preview text.", res.text)
        # Verify data attributes and absence of inline XSS onclicks (BUG-008)
        self.assertIn('data-debtor-id="DEB-DASH-01"', res.text)
        self.assertNotIn("onclick=\"approveItem('DEB-DASH-01')\"", res.text)
        self.assertNotIn("onclick=\"rejectItem('DEB-DASH-01')\"", res.text)

    def test_multiple_items_queued_same_debtor_and_contact_forwarding(self):
        msg1 = DraftedMessage(
            debtor_id="DEB-MULTI",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="First Notice",
            body="First body.",
            is_statutory=False,
            dark_pattern_clean=True,
        )
        msg2 = DraftedMessage(
            debtor_id="DEB-MULTI",
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            subject="Second Notice",
            body="Second body.",
            is_statutory=True,
            dark_pattern_clean=True,
        )
        queue_for_review(
            debtor_id="DEB-MULTI",
            debtor_name="Multi Debtor",
            strategy="REQUEST_PAYMENT",
            ask_amount_paise=10000_00,
            reasoning="First queue action",
            draft=msg1,
            recipient_email="finance@multi.com",
            recipient_phone="+919876500001",
        )
        queue_for_review(
            debtor_id="DEB-MULTI",
            debtor_name="Multi Debtor",
            strategy="ESCALATE",
            ask_amount_paise=20000_00,
            reasoning="Second queue action",
            draft=msg2,
            recipient_email="legal@multi.com",
            recipient_phone="+919876500002",
        )

        queue = get_review_queue()
        multi_items = [i for i in queue if i["debtor_id"] == "DEB-MULTI"]
        self.assertEqual(len(multi_items), 2)
        self.assertEqual(multi_items[0]["strategy"], "REQUEST_PAYMENT")
        self.assertEqual(multi_items[0]["recipient_email"], "finance@multi.com")
        self.assertEqual(multi_items[1]["strategy"], "ESCALATE")
        self.assertEqual(multi_items[1]["recipient_email"], "legal@multi.com")

        # Approve first item
        res1 = self.client.post("/api/operator/approve", json={"debtor_id": "DEB-MULTI"})
        self.assertEqual(res1.status_code, 200)
        self.assertTrue(res1.json()["approved"])

        # Second item should still remain
        queue2 = get_review_queue()
        multi_items2 = [i for i in queue2 if i["debtor_id"] == "DEB-MULTI"]
        self.assertEqual(len(multi_items2), 1)
        self.assertEqual(multi_items2[0]["strategy"], "ESCALATE")

        # Approve second item
        res2 = self.client.post("/api/operator/approve", json={"debtor_id": "DEB-MULTI"})
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.json()["approved"])

        # Queue should now be empty
        self.assertEqual(len(get_review_queue()), 0)

    def test_unauthorized_operator_access_returns_401(self):
        unauthed_client = TestClient(app)

        # GET /operator
        res = unauthed_client.get("/operator")
        self.assertEqual(res.status_code, 401)

        # POST /api/operator/kill-switch
        res = unauthed_client.post("/api/operator/kill-switch", json={"active": True})
        self.assertEqual(res.status_code, 401)

        # GET /api/operator/queue
        res = unauthed_client.get("/api/operator/queue")
        self.assertEqual(res.status_code, 401)

        # POST /api/operator/approve
        res = unauthed_client.post("/api/operator/approve", json={"debtor_id": "DEB-001"})
        self.assertEqual(res.status_code, 401)

        # POST /api/operator/reject
        res = unauthed_client.post("/api/operator/reject", json={"debtor_id": "DEB-001"})
        self.assertEqual(res.status_code, 401)

        # GET /api/operator/export
        res = unauthed_client.get("/api/operator/export")
        self.assertEqual(res.status_code, 401)

    def test_authorized_operator_access_methods(self):
        # 1. Bearer token
        bearer_client = TestClient(app)
        bearer_client.headers["Authorization"] = "Bearer operator-secret-key"
        res = bearer_client.get("/api/operator/queue")
        self.assertEqual(res.status_code, 200)

        # 2. Query param ?key=...
        query_client = TestClient(app)
        res = query_client.get("/api/operator/queue?key=operator-secret-key")
        self.assertEqual(res.status_code, 200)

        # 3. Query param ?api_key=...
        res = query_client.get("/api/operator/queue?api_key=operator-secret-key")
        self.assertEqual(res.status_code, 200)

        # 4. HTML /operator with query param
        res = query_client.get("/operator?key=operator-secret-key")
        self.assertEqual(res.status_code, 200)

    def test_review_action_reason_length_validation(self):
        # 2000 chars should be accepted
        valid_reason = "A" * 2000
        res = self.client.post(
            "/api/operator/reject",
            json={"debtor_id": "NON-EXISTENT", "reason": valid_reason},
        )
        self.assertEqual(res.status_code, 404)  # Passes schema validation, fails on debtor lookup

        # 2001 chars should fail with 422 Unprocessable Entity
        invalid_reason = "A" * 2001
        res_invalid = self.client.post(
            "/api/operator/reject",
            json={"debtor_id": "NON-EXISTENT", "reason": invalid_reason},
        )
        self.assertEqual(res_invalid.status_code, 422)

    def test_export_content_disposition_header(self):
        res_json = self.client.get("/api/operator/export?format=json")
        self.assertEqual(res_json.status_code, 200)
        self.assertEqual(
            res_json.headers.get("content-disposition"),
            'attachment; filename="audit_events.json"',
        )

        res_csv = self.client.get("/api/operator/export?format=csv")
        self.assertEqual(res_csv.status_code, 200)
        self.assertEqual(
            res_csv.headers.get("content-disposition"),
            'attachment; filename="audit_events.csv"',
        )


if __name__ == "__main__":
    unittest.main()

