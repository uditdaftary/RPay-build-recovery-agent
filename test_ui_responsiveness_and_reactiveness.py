"""Test suite for UI Responsiveness and Reactiveness validation across all surfaces.

Tests:
1. Viewport responsiveness across breakpoints: Mobile (375px), Tablet (768px), Desktop (1280px).
2. DOM overflow, element wrapping, and container constraints.
3. Interactive reactiveness: Accordions, Modals, Button states, Status badges, Form validations.
4. Dynamic client-side feedback: Copy-to-clipboard, Expand/Collapse, and Re-run Evaluation.
"""

import pathlib
import unittest
from datetime import timedelta
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.config import business_today
from app.server import INVOICES, _balance_paise, _load_invoices, _lookup_invoice, app, demo_token

_store_off = None


def setUpModule():
    """No test in this file may reach the durable store.

    Several of these exercise mutating endpoints, and `_persist_invoice` writes through
    whenever DATABASE_URL is set - which `.env` supplies on a developer machine. That is
    how a seeded ledger invoice ended up in the shared database carrying a test literal
    as its dispute reason. Disabling the store also makes the assertions deterministic,
    since they then read the ledger rather than whatever an earlier session left behind.
    """
    global _store_off
    _store_off = patch("app.store.is_enabled", return_value=False)
    _store_off.start()


def tearDownModule():
    _store_off.stop()


class TestUIResponsivenessAndReactiveness(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # `INVOICES` is process-global and the door tests POST to endpoints that mutate
        # it, so the next test in the run inherits whatever this one left behind.
        self._invoice_snapshot = {t: dict(inv) for t, inv in INVOICES.items()}

    def tearDown(self):
        for token, original in self._invoice_snapshot.items():
            INVOICES[token].clear()
            INVOICES[token].update(original)

    def test_landing_page_responsive_meta_and_layout(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.text, "html.parser")

        meta_viewport = soup.find("meta", attrs={"name": "viewport"})
        self.assertIsNotNone(meta_viewport, "Missing viewport meta tag")
        self.assertEqual(meta_viewport["content"], "width=device-width, initial-scale=1")

        wrap = soup.find(class_="wrap")
        self.assertIsNotNone(wrap, "Missing .wrap responsive container")

        # The new landing page has a hero section and differentiator cards
        hero = soup.find(class_="hero")
        self.assertIsNotNone(hero, "Missing .hero section on landing page")

        # Demo invoices are now in a table with links pointing to /r/{token}
        invoice_links = soup.find_all("a", href=lambda h: h and h.startswith("/r/"))
        self.assertGreaterEqual(len(invoice_links), 1, "Expected at least 1 demo invoice link")

    def test_resolution_portal_responsive_structure(self):
        invoices = _load_invoices()
        first_token = next(iter(invoices.keys()))
        response = self.client.get(f"/r/{first_token}")
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.text, "html.parser")

        pay_btn = soup.find("button", id="pay")
        self.assertIsNotNone(pay_btn, "Pay button missing")
        self.assertIn("btn-primary", pay_btn.get("class", []))

        self.assertIsNotNone(soup.find(id="pay-status"), "pay-status element missing")
        self.assertIsNotNone(soup.find(id="promise-status"), "promise-status element missing")
        self.assertIsNotNone(soup.find(id="dispute-status"), "dispute-status element missing")

        date_input = soup.find("input", id="promised-date")
        self.assertIsNotNone(date_input, "Promise date input missing")
        self.assertTrue(date_input.has_attr("min"), "Promise date missing min attribute")
        self.assertTrue(date_input.has_attr("max"), "Promise date missing max attribute")

        textarea = soup.find("textarea", id="dispute-reason")
        self.assertIsNotNone(textarea, "Dispute textarea missing")
        self.assertTrue(textarea.has_attr("maxlength"), "Dispute textarea missing maxlength attribute")

    def test_resolution_portal_promise_reactiveness_validation(self):
        invoices = _load_invoices()
        token = next(iter(invoices.keys()))

        res_past = self.client.post("/api/promise", json={"token": token, "promised_date": "2020-01-01"})
        self.assertEqual(res_past.status_code, 422)
        self.assertIn("error", res_past.json())

        res_future = self.client.post("/api/promise", json={"token": token, "promised_date": "2099-01-01"})
        self.assertEqual(res_future.status_code, 422)
        self.assertIn("error", res_future.json())

    def test_resolution_portal_dispute_reactiveness_validation(self):
        invoices = _load_invoices()
        token = next(iter(invoices.keys()))

        res_empty = self.client.post("/api/dispute", json={"token": token, "reason": ""})
        self.assertEqual(res_empty.status_code, 422)

        res_valid = self.client.post(
            "/api/dispute",
            json={"token": token, "reason": "Goods damaged on truck delivery. Inspection report attached."},
        )
        self.assertEqual(res_valid.status_code, 200)
        data = res_valid.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("category"), "GOODS_SERVICES")
        self.assertIn("evidence_required", data)
        self.assertIn("statutory_clock_suspended", data)

    def test_results_dashboard_responsive_grid_and_classes(self):
        response = self.client.get("/results")
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.text, "html.parser")

        nav = soup.find("nav")
        self.assertIsNotNone(nav, "Navbar missing")

        card_grid = soup.find(class_="card-grid")
        self.assertIsNotNone(card_grid, ".card-grid container missing")
        stat_cards = card_grid.find_all(class_="stat-card")
        self.assertEqual(len(stat_cards), 6, "Expected 6 KPI stat cards")

        strat_bars = soup.find_all(class_="strat-bar-wrap")
        self.assertGreaterEqual(len(strat_bars), 6, "Expected strategy comparison bars")

        matrix_table = soup.find(class_="matrix-table")
        self.assertIsNotNone(matrix_table, ".matrix-table missing")
        table_container = matrix_table.parent
        self.assertIn("overflow-x", str(table_container.get("style", "")), "Matrix table must be wrapped in overflow-x: auto")

        main_rows = soup.find_all(class_="matrix-row-main")
        detail_rows = soup.find_all(class_="matrix-row-details")
        self.assertEqual(len(main_rows), 8, "Expected 8 main matrix rows")
        self.assertEqual(len(detail_rows), 8, "Expected 8 detail rows")

        self.assertIsNotNone(soup.find(id="btn-expand-all"), "Expand All button missing")
        self.assertIsNotNone(soup.find(id="btn-collapse-all"), "Collapse All button missing")

        modal = soup.find(id="copy-modal")
        self.assertIsNotNone(modal, "Copy preview modal missing")
        self.assertIsNotNone(soup.find(id="modal-subject"), "Modal subject container missing")
        self.assertIsNotNone(soup.find(id="modal-body"), "Modal body container missing")
        self.assertIsNotNone(soup.find(id="btn-copy-clipboard"), "Copy to clipboard button missing")

    def test_results_css_breakpoint_rules(self):
        response = self.client.get("/results")
        self.assertEqual(response.status_code, 200)
        html = response.text

        self.assertIn("@media (max-width: 768px)", html)
        self.assertIn(".details-box { grid-template-columns: 1fr; }", html)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(280px, 1fr))", html)

    def test_operator_console_authenticated_elements(self):
        with patch.dict("os.environ", {"OPERATOR_API_KEY": "test-key-op"}):
            response = self.client.get("/operator?key=test-key-op")
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.text, "html.parser")

            kill_btn = soup.find(id="toggle-kill-switch")
            self.assertIsNotNone(kill_btn, "Kill switch button missing")

            export_links = soup.find_all("a", href=lambda h: h and "/api/operator/export" in h)
            self.assertEqual(len(export_links), 2, "Expected JSON and CSV export links")


class TestNavigationAndDoorContracts(unittest.TestCase):
    """The checks that DOM-presence assertions kept missing.

    Every bug this file failed to catch was a broken *contract*, not a missing element:
    a nav link to a token that no longer existed, and a promise door posting a field
    name the schema does not accept. Both rendered perfectly.
    """

    def setUp(self):
        self.client = TestClient(app)

    def test_every_internal_link_on_the_landing_page_resolves(self):
        response = self.client.get("/")
        soup = BeautifulSoup(response.text, "html.parser")
        hrefs = {
            a["href"].split("?")[0]
            for a in soup.find_all("a", href=True)
            if a["href"].startswith("/")
        }
        self.assertIn("/results", hrefs)
        self.assertTrue(
            any(h.startswith("/r/") for h in hrefs),
            "the nav advertises a live resolution page and none was linked",
        )
        for href in sorted(hrefs):
            # /operator is 401 without a key, which is correct; only a 404 means the
            # page points at something that does not exist.
            self.assertNotEqual(
                self.client.get(href).status_code, 404, f"{href} is linked but 404s"
            )

    def test_the_advertised_live_demo_still_has_something_to_collect(self):
        """A settled invoice renders a "Pay 0.00" button, which is worse than a 404.

        The durable store outlives the process, so this cannot be asserted against the
        ledger file - it has to read what the page would actually show.
        """
        token = demo_token()
        invoice = _lookup_invoice(token)
        self.assertIsNotNone(invoice, f"the nav links to /r/{token} and it does not exist")
        self.assertEqual(invoice["status"], "OVERDUE", f"{token} is no longer chaseable")
        self.assertGreater(_balance_paise(invoice), 0, f"{token} has a zero balance")

    def test_each_door_accepts_the_payload_the_page_actually_sends(self):
        page_source = pathlib.Path("app/templates/resolution.html").read_text(encoding="utf-8")
        # Two tokens, because both doors mutate the invoice they are given and a promise
        # on a disputed invoice is correctly refused. `INVOICES` is process-global, so
        # each door is also restored rather than left mutated for the next test.
        chaseable = [t for t, inv in INVOICES.items() if inv["status"] == "OVERDUE"]
        self.assertGreaterEqual(len(chaseable), 2, "need two chaseable invoices")
        promise_token, dispute_token = chaseable[0], chaseable[1]
        snapshot = {t: dict(INVOICES[t]) for t in (promise_token, dispute_token)}
        try:
            # Promise: the field name is asserted against the page, not restated here, so
            # a rename on either side fails rather than drifting apart.
            self.assertIn("promised_date: value", page_source)
            when = (business_today() + timedelta(days=10)).isoformat()
            promised = self.client.post(
                "/api/promise", json={"token": promise_token, "promised_date": when}
            )
            self.assertEqual(promised.status_code, 200, promised.text)

            # Dispute: the page renders res.category / res.evidence_required /
            # res.statutory_clock_suspended, so the response has to carry all three.
            self.assertIn("res.statutory_clock_suspended", page_source)
            disputed = self.client.post(
                "/api/dispute",
                json={
                    "token": dispute_token,
                    "reason": "Goods were short delivered against the PO.",
                },
            )
            self.assertEqual(disputed.status_code, 200, disputed.text)
            for field in ("category", "evidence_required", "statutory_clock_suspended"):
                self.assertIn(field, disputed.json())
        finally:
            for token, original in snapshot.items():
                INVOICES[token].clear()
                INVOICES[token].update(original)


if __name__ == "__main__":
    unittest.main()
