"""Durable-state integration checks.

Run against a real engine rather than SQLite: the schema uses `jsonb`, `bigserial`,
`ON CONFLICT` and `FOR UPDATE SKIP LOCKED`, none of which SQLite would exercise
faithfully.

**These tests TRUNCATE every table, so they deliberately do not read `DATABASE_URL`.**
That variable points at whatever database the application is configured with - which,
once a deployment exists, is production. A destructive suite that auto-targets the app's
own database destroys the audit log the moment someone runs `pytest` on a machine with a
populated `.env`. `STORE_TEST_DATABASE_URL` has to be set separately and on purpose.

    docker run -d --name recovery-pg -e POSTGRES_PASSWORD=devpass \
        -e POSTGRES_DB=recovery -p 55432:5432 postgres:16-alpine
    STORE_TEST_DATABASE_URL=postgresql://postgres:devpass@localhost:55432/recovery \
        python -m pytest test_store.py
"""

import os
import unittest
from unittest.mock import patch

from app import audit, store
from run_experiment import isolated_audit_log

DSN = os.getenv("STORE_TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(DSN, "STORE_TEST_DATABASE_URL is not set; durable-state checks skipped")
class TestDurableState(unittest.TestCase):
    def setUp(self) -> None:
        # Point the module at the throwaway database for the duration of each test, so
        # nothing here can reach whatever DATABASE_URL happens to name.
        self.enterContext(patch.dict(os.environ, {"DATABASE_URL": DSN}))
        store.reset_for_tests()
        self.addCleanup(store.reset_for_tests)
        pool = store._connect()
        with pool.connection() as conn:
            conn.execute(
                "TRUNCATE audit_events, invoice_runtime, operator_state, review_queue"
            )

    def test_audit_events_survive_a_process_restart(self) -> None:
        """The log is an input to every decision, so it has to outlive the instance."""
        audit.record("promise.made", debtor_id="DEB-001", promised_date="2026-09-10")
        audit.record("settlement.confirmed", debtor_id="DEB-001", invoice_id="INV-1")

        # A cold start: new pool, no in-process memory of anything written above.
        store.reset_for_tests()

        events = audit.read_all()
        self.assertEqual([e["event"] for e in events], ["promise.made", "settlement.confirmed"])
        self.assertEqual(events[0]["promised_date"], "2026-09-10")

    def test_settlement_claim_is_idempotent_across_instances(self) -> None:
        """Razorpay redelivers. Two instances must not both credit one capture."""
        self.assertTrue(store.claim_payment("INV-1", "pay_abc"))
        store.reset_for_tests()  # the redelivery lands on a different instance
        self.assertFalse(store.claim_payment("INV-1", "pay_abc"))
        # A genuinely different capture on the same invoice still goes through.
        self.assertTrue(store.claim_payment("INV-1", "pay_def"))

    def test_kill_switch_is_visible_to_every_instance(self) -> None:
        from app.operator import is_kill_switch_active, set_kill_switch

        set_kill_switch(True)
        store.reset_for_tests()
        self.assertTrue(is_kill_switch_active())

        set_kill_switch(False)
        store.reset_for_tests()
        self.assertFalse(is_kill_switch_active())

    def test_review_queue_survives_and_requeues_at_the_head(self) -> None:
        store.enqueue_review("DEB-001", {"strategy": "ESCALATE", "n": 1})
        store.enqueue_review("DEB-001", {"strategy": "ESCALATE", "n": 2})
        store.reset_for_tests()

        first = store.pop_review_item("DEB-001")
        self.assertEqual(first["n"], 1)

        # What a failed dispatch does: the item goes back ahead of everything queued.
        store.requeue_review_item("DEB-001", first)
        self.assertEqual([i["n"] for i in store.list_review_queue()], [1, 2])

    def test_invoice_lifecycle_round_trips(self) -> None:
        store.save_invoice_runtime("INV-1", {"status": "PARTIALLY_PAID", "amount_received_paise": 500})
        store.save_invoice_runtime("INV-1", {"status": "PAID", "amount_received_paise": 1000})
        loaded = store.load_invoice_runtime()
        self.assertEqual(loaded["INV-1"], {"status": "PAID", "amount_received_paise": 1000})

    def test_isolated_runs_never_touch_the_database(self) -> None:
        """The benchmark must stay a pure function of its seed even with a store configured."""
        audit.record("decision.made", debtor_id="DEB-REAL", strategy="WAIT")

        with isolated_audit_log():
            audit.record("decision.made", debtor_id="DEB-SANDBOX", strategy="ESCALATE")
            inside = [e["debtor_id"] for e in audit.read_all()]

        self.assertEqual(inside, ["DEB-SANDBOX"], "isolated run read or wrote shared state")
        after = [e["debtor_id"] for e in audit.read_all()]
        self.assertEqual(after, ["DEB-REAL"], "sandbox row leaked into the durable log")


if __name__ == "__main__":
    unittest.main()
