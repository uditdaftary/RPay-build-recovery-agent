import unittest
from datetime import date
from unittest.mock import patch

from app import audit
from app.envelope import ActionClass, Channel, Language, Strategy, Tone
from app.ledger import generate
from app.operator import set_kill_switch
from app.pipeline import PipelineRunResult, execute_recovery_pipeline
from app.strategist import StrategistDecision
from run_experiment import isolated_audit_log


class TestPipelineOrchestration(unittest.TestCase):
    def setUp(self):
        set_kill_switch(False)
        self.enterContext(isolated_audit_log())
        self.ledger = generate(seed=42)
        # The seeded ledger carries no contact fields, and the generator must not grow any:
        # its seed-42 fingerprint is the reproducibility claim that verify_all.py Gate 3
        # enforces. Contacts are injected here so the tests below can exercise the dispatch
        # path at all. What the SHIPPED ledger actually does is asserted separately, in
        # test_seeded_ledger_dispatches_nothing_without_contacts, so this fixture cannot
        # quietly stand in for production behaviour.
        for i, d in enumerate(self.ledger["debtors"]):
            d["email"] = f"{d['debtor_id'].lower()}@customercorp.in"
            d["phone"] = f"+9198000000{i:02d}"

    def test_seeded_ledger_dispatches_nothing_without_contacts(self):
        """The shipped ledger has no email or phone, so nothing may go out unattended.

        Removing the fabricated-recipient fallback made this the real end-to-end behaviour:
        every contactable decision routes to a human instead of guessing an address. That is
        the intended outcome, but it is a property worth failing on if it changes silently -
        either because contacts were added to the generator or because address synthesis
        came back.
        """
        pristine = generate(seed=42)
        self.assertNotIn("email", pristine["debtors"][0])
        self.assertNotIn("phone", pristine["debtors"][0])

        result = execute_recovery_pipeline(pristine, as_of=date(2026, 8, 26), dry_run=True)

        self.assertEqual(result.automated_dispatches, 0)
        self.assertEqual(result.total_evaluated, 20)
        self.assertEqual(result.suppressed_opt_out, 1)
        # 18 rather than 19: one debtor's decision carries Channel.NONE (a WAIT or a
        # handoff reaches nobody), so it is neither dispatched nor queued.
        self.assertEqual(result.review_queued, 18)

        sent = [
            e for e in audit.read_all()
            if e.get("event") in ("channel.email_sent", "channel.whatsapp_sent")
        ]
        self.assertEqual(sent, [])

    def test_full_pipeline_run_routes_correctly(self):
        result = execute_recovery_pipeline(self.ledger, as_of=date(2026, 8, 26), dry_run=True)
        self.assertIsInstance(result, PipelineRunResult)
        self.assertEqual(result.total_evaluated, 20)
        self.assertGreater(result.automated_dispatches, 0)
        self.assertGreater(result.review_queued, 0)
        self.assertEqual(result.suppressed_opt_out, 1)

    def test_kill_switch_halts_pipeline_dispatch(self):
        set_kill_switch(True)
        result = execute_recovery_pipeline(self.ledger, as_of=date(2026, 8, 26), dry_run=True)
        self.assertEqual(result.automated_dispatches, 0)
        self.assertGreater(result.kill_switch_blocked, 0)

    def test_temporal_as_of_propagated_to_batch_evaluation(self):
        target_date = date(2026, 9, 15)
        result = execute_recovery_pipeline(self.ledger, as_of=target_date, dry_run=True)
        self.assertEqual(self.ledger.get("as_of"), "2026-09-15")
        self.assertIsInstance(result, PipelineRunResult)

    def test_unmapped_merchant_fails_closed_to_review_queue(self):
        # When a decision references a debtor with missing or unmapped merchant_id
        fake_decision = StrategistDecision(
            debtor_id="DEB-001",
            debtor_name="Acme Corp",
            strategy=Strategy.REQUEST_PAYMENT,
            channel=Channel.EMAIL,
            language=Language.EN,
            tone=Tone.FORMAL,
            ask_amount_paise=50000_00,
            reasoning="Overdue invoice chasing",
            confidence=0.9,
            review_required=False,
            action_class=ActionClass.AUTOMATABLE,
        )

        for d in self.ledger["debtors"]:
            if d["debtor_id"] == "DEB-001":
                d["merchant_id"] = "MER-UNKNOWN"
                break

        with patch("run_experiment.run_deterministic_agent_batch", return_value=[fake_decision]):
            result = execute_recovery_pipeline(self.ledger, as_of=date(2026, 8, 26), dry_run=True)

        self.assertEqual(result.automated_dispatches, 0)
        self.assertEqual(result.review_queued, 1)

        events = audit.read_all()
        unresolved_events = [
            e for e in events
            if e.get("event") == "pipeline.merchant_unresolved" and e.get("debtor_id") == "DEB-001"
        ]
        self.assertEqual(len(unresolved_events), 1)
        self.assertEqual(unresolved_events[0].get("merchant_id"), "MER-UNKNOWN")

    def test_debtor_contact_forwarded_to_dispatch(self):
        # Set custom contact details on an active debtor
        target_debtor = None
        for d in self.ledger["debtors"]:
            if not d.get("opted_out"):
                d["email"] = "custom.finance@debtorcorp.in"
                d["phone"] = "+919888877777"
                target_debtor = d
                break

        self.assertIsNotNone(target_debtor)
        execute_recovery_pipeline(self.ledger, as_of=date(2026, 8, 26), dry_run=True)

        events = audit.read_all()
        sent_events = [
            e for e in events
            if e.get("debtor_id") == target_debtor["debtor_id"] and e.get("event") in ("channel.email_sent", "channel.whatsapp_sent")
        ]
        if sent_events:
            self.assertIn(sent_events[-1].get("recipient"), ("custom.finance@debtorcorp.in", "+919888877777"))

    def test_missing_contact_routes_to_human_review_without_synthesis(self):
        target_debtor = None
        for d in self.ledger["debtors"]:
            if not d.get("opted_out"):
                d["email"] = None
                d["recipient_email"] = None
                d["phone"] = None
                d["recipient_phone"] = None
                target_debtor = d
                break

        self.assertIsNotNone(target_debtor)
        execute_recovery_pipeline(self.ledger, as_of=date(2026, 8, 26), dry_run=True)

        events = audit.read_all()
        # Verify no fake emails or fake phones were dispatched for this debtor
        sent_events = [
            e for e in events
            if e.get("debtor_id") == target_debtor["debtor_id"] and e.get("event") in ("channel.email_sent", "channel.whatsapp_sent")
        ]
        self.assertEqual(len(sent_events), 0)

    def test_opted_out_debtor_suppressed_completely(self):
        opted_out_ids = [d["debtor_id"] for d in self.ledger["debtors"] if d.get("opted_out")]
        self.assertTrue(len(opted_out_ids) > 0)
        execute_recovery_pipeline(self.ledger, as_of=date(2026, 8, 26), dry_run=True)

        events = audit.read_all()
        for opt_id in opted_out_ids:
            # Opted out debtor should never have automated dispatches or reviews queued
            dispatched = [
                e for e in events
                if e.get("debtor_id") == opt_id and e.get("event") in ("channel.email_sent", "channel.whatsapp_sent", "operator.review_queued")
            ]
            self.assertEqual(len(dispatched), 0)
            opt_out_logged = [
                e for e in events
                if e.get("debtor_id") == opt_id and e.get("event") == "pipeline.debtor_opted_out"
            ]
            self.assertEqual(len(opt_out_logged), 1)


if __name__ == "__main__":
    unittest.main()
