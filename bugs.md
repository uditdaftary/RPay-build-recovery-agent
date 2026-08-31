# Bug Report — recovery-agent PR #8 — 2026-08-31

## Summary
- Critical: 0 open, 9 fixed
- Intermediate: 0 open, 20 fixed
- Normal: 0 open, 19 fixed
- Total Open Findings: 0 (100% Resolved)
- Total Resolved Findings: 48

---

## 🔴 Critical
*(All 9 Critical findings resolved and verified.)*

---

## 🟡 Intermediate
*(All 20 Intermediate findings resolved and verified.)*

---

## 🟢 Normal
*(All 19 Normal findings resolved and verified.)*

---

## ✅ Resolved

### BUG-034: Audit Log State Bleed on `/results` & `/api/results` Message Preview Drafting — Fixed 2026-08-31
- **File:** `app/server.py:851-905`
- **Resolution:** Wrapped entire benchmark evaluation and copy preview drafting data preparation block inside `with isolated_audit_log():`, preventing preview drafting from recording events to the live production `audit/events.jsonl` log.

### BUG-035: Non-Constant-Time Operator Secret Key Comparison on `/operator` Dashboard View — Fixed 2026-08-31
- **File:** `app/server.py:722`
- **Resolution:** Replaced naive string inequality with `hmac.compare_digest(key, expected)` in `operator_dashboard()`, mitigating timing side-channel attacks.

### BUG-036: Missing Query Parameter Forwarding from `/results` Navbar to `/operator` Console — Fixed 2026-08-31
- **File:** `app/templates/results.html:202` & `app/server.py:953-971`
- **Resolution:** Extracted `operator_key` from request and passed to template context in `results_page()`, updating `/results` navbar link to `<a href="/operator{% if operator_key %}?key={{ operator_key }}{% endif %}">`.

### BUG-037: Inline `<script>` Template Variable Interpolation Allows Script Tag Breakout in Operator Console — Fixed 2026-08-31
- **File:** `app/templates/operator.html:126`
- **Resolution:** Replaced string interpolation with Jinja2's `| tojson` filter (`const operatorKey = {{ (operator_key | default('')) | tojson }};`), eliminating script tag breakout vulnerabilities.

### BUG-038: Unhandled Jinja2 `TypeError` Crash on Null `item.verdict` in Results Evaluation Matrix — Fixed 2026-08-31
- **File:** `app/templates/results.html:478`
- **Resolution:** Added defensive null checks `{% if item.verdict and 'Agent Win' in item.verdict %}` to prevent `TypeError` when `item.verdict` is `None`.

### BUG-039: Inline Attribute JavaScript String Breakout via Unsanitized `debtor_id` in Results Table Details Toggle — Fixed 2026-08-31
- **File:** `app/templates/results.html:455, 493`
- **Resolution:** Removed inline `onclick` string interpolation, added `data-debtor-id` attributes, and bound event listeners via JavaScript.

### BUG-040: `simulate_mandate_webhook` Unhandled `ValueError` / 500 Internal Server Error on Invalid `failure_code` Input — Fixed 2026-08-31
- **File:** `app/server.py:657-667`
- **Resolution:** Annotated `failure_code: MandateFailureCode` on `MandateWebhookSimulateRequest`, allowing Pydantic to validate the enum and return standard HTTP 422 JSON validation errors.

### BUG-041: `evaluate_section_43b_h` Direct Subscript `merchant["merchant_id"]` Crashes with `KeyError` on Unresolved Merchant Fallback — Fixed 2026-08-31
- **File:** `app/pipeline.py:97` & `app/statute.py:228-236`
- **Resolution:** Safely extracted merchant dictionary keys using `.get()` in `evaluate_section_43b_h()` and provided explicit fallback merchant dictionary in `pipeline.py`.

### BUG-042: `isolated_audit_log()` Mutates Global Module Variables Non-Atomically in Multi-Threaded Runtime — Fixed 2026-08-31
- **File:** `app/audit.py:17-35` & `run_experiment.py:41-55`
- **Resolution:** Added `contextvars.ContextVar` support (`_current_audit_dir`, `_current_event_log`) in `app/audit.py` to route audit log events per-context safely in multi-threaded runtimes.

### BUG-043: Missing Debtor Merchant ID Silently Defaults to First Available Merchant in `get_results_data` — Fixed 2026-08-31
- **File:** `app/server.py:872-873`
- **Resolution:** Replaced `"MER-001"` default with explicit unmapped merchant fallback dictionary (`{"merchant_id": "UNKNOWN", "name": "Supplier", "udyam_registered": False}`).

### BUG-044: Unhandled `ValueError` on Malformed `as_of` Date Query Parameter in `/api/results` — Fixed 2026-08-31
- **File:** `app/server.py:932, 949`
- **Resolution:** Validated `as_of` parameter and caught `ValueError` from `date.fromisoformat()`, returning HTTP 400 Bad Request.

### BUG-045: Operator Console Lacks User-Facing Error Alerts on Non-200 / 4xx API Rejections — Fixed 2026-08-31
- **File:** `app/templates/operator.html:150-156, 183-203`
- **Resolution:** Added `res.ok` status validation and user-facing alert notifications on approval/rejection failures.

### BUG-046: Unhandled `TypeError` on Insecure HTTP Context Accessing `navigator.clipboard` — Fixed 2026-08-31
- **File:** `app/templates/results.html:694-703`
- **Resolution:** Added capability check for `navigator.clipboard` with fallback to `document.execCommand('copy')` via hidden textarea.

### BUG-047: Missing Fallback Filter for Null WhatsApp / SMS Subject Lines Displays "Subject: None" in Evaluation Card Preview — Fixed 2026-08-31
- **File:** `app/templates/results.html:536`
- **Resolution:** Applied Jinja2 `default` filter: `{{ item.drafted_copy_preview.subject | default('(No Subject - WhatsApp Template)', true) }}`.

### BUG-048: External Navigation Links Lack `rel="noopener noreferrer"` Reverse Tabnabbing Protection — Fixed 2026-08-31
- **File:** `app/templates/results.html:204` & `app/templates/operator.html:19`
- **Resolution:** Added `rel="noopener noreferrer"` to all `target="_blank"` anchor tags across both templates.

### BUG-001: Hinglish Statutory Escalation Notice Displays Interest Only as Total Payable Amount — Fixed 2026-08-31
- **File:** `app/messages.py:185`
- **Resolution:** Replaced `{interest_disp}` with `{total_payable_disp}` formatting `interest_res.total_payable_paise`.

### BUG-002: Unhandled TypeError Crash on Null `contractual_due_date` During Statutory Due Date Parsing — Fixed 2026-08-31
- **File:** `app/messages.py:173`
- **Resolution:** Coalesced `raw_due = primary_invoice.get("contractual_due_date") or as_of.isoformat()`.

### BUG-003: False MSMED Ineligibility Refusal and Audit Log Corruption for Invoices Overdue <= 15 Days — Fixed 2026-08-31
- **File:** `app/messages.py:170-208`
- **Resolution:** Set `refusal_reason = stat_eval.refusal_reason or "Invoice overdue days <= 15 grace threshold"`.

### BUG-004: Review Queue Pop Before Kill Switch Check Causes Irreversible Data Loss — Fixed 2026-08-31
- **File:** `app/operator.py:88-99`
- **Resolution:** Verified `_KILL_SWITCH_ACTIVE` under `_OPERATOR_LOCK` prior to popping items from `_REVIEW_QUEUE`.

### BUG-005: Case-Sensitive String Matching in `msmed_eligible` Permits Ineligible Traders to Send Statutory Notices — Fixed 2026-08-31
- **File:** `app/ledger.py:86-90`
- **Resolution:** Lowercased and stripped category and activity strings in `msmed_eligible`.

### BUG-006: Unhandled KeyError on Empty Invoices List in Statutory Evaluation — Fixed 2026-08-31
- **File:** `app/messages.py:62-64` & `app/ledger.py:214`
- **Resolution:** Updated `balance_paise` and `invoice_collectible_paise` to use `invoice.get("amount_paise", 0)`.

### BUG-007: Fallback Delivery Date Heuristic in Dispute Endpoint Invalidates MSMED Section 15 Statutory Clock Suspension — Fixed 2026-08-31
- **File:** `app/server.py:590-594`
- **Resolution:** Implemented multi-tier fallback prioritizing `invoice_date`, 45d/15d written agreement adjustment on `contractual_due_date`, and `days_overdue`.

### BUG-008: DOM / Inline Attribute-Based XSS in Operator Console Button Onclick Handlers — Fixed 2026-08-31
- **File:** `app/templates/operator.html:63-64`
- **Resolution:** Replaced inline `onclick` handlers with `data-debtor-id` attributes and event listener attachment.

### BUG-009: Falsy Zero Overwrite Replaces Explicit ₹0 Ask Amounts with Naive Invoice Totals — Fixed 2026-08-31
- **File:** `app/messages.py:64`
- **Resolution:** Changed to explicit None check: `decision.ask_amount_paise if decision.ask_amount_paise is not None else sum(...)`.

### BUG-010: Temporal Decoupling: Pipeline Ignores Caller's `as_of` Date During Strategist Batch Evaluation — Fixed 2026-08-31
- **File:** `app/pipeline.py:45`
- **Resolution:** Set `ledger["as_of"] = as_of.isoformat()` before invoking batch evaluation.

### BUG-011: Missing Debtor Merchant ID Silently Defaults to Eligible MSME Supplier — Fixed 2026-08-31
- **File:** `app/pipeline.py:59`
- **Resolution:** Removed `"MER-001"` default, logging `pipeline.merchant_unresolved` and routing to human review queue fail-closed.

### BUG-012: Pipeline & Operator Queue Dispatch Drops Debtor Contact Details and Dispatches to Placeholder Addresses — Fixed 2026-08-31
- **File:** `app/pipeline.py:87` & `app/operator.py:54-70, 102-113`
- **Resolution:** Extracted debtor email/phone, forwarded through `DraftedMessage`, `queue_for_review`, and `dispatch_message`.

### BUG-013: `Channel.PORTAL` Messages Silently Suppressed as No-Contact — Fixed 2026-08-31
- **File:** `app/channels.py:181-201`
- **Resolution:** Added dedicated handler for `Channel.PORTAL` logging `channel.portal_notification_created`.

### BUG-014: CSV Audit Log Exporter Drops Critical Compliance Audit Fields Due to Hardcoded Fieldnames and Key Mismatch — Fixed 2026-08-31
- **File:** `app/operator.py:144-149`
- **Resolution:** Dynamically collected all unique field keys across all audit event records before writing CSV headers.

### BUG-015: `POST /api/operator/approve` Returns HTTP 200 OK When Approval is Blocked by Kill Switch — Fixed 2026-08-31
- **File:** `app/server.py:682-686`
- **Resolution:** Added `status_code=409` when `res.get("approved") is False`.

### BUG-016: Missing `POST /api/mandate/simulate-webhook` Endpoint in HTTP Server — Fixed 2026-08-31
- **File:** `app/server.py:1-706`
- **Resolution:** Added and mounted `POST /api/mandate/simulate-webhook` returning `MandateRetryPlan`.

### BUG-017: Operator Console and API Endpoints Lack Authentication, Authorization, and CSRF Protection — Fixed 2026-08-31
- **File:** `app/server.py:653-705`
- **Resolution:** Added `verify_operator_auth` dependency validating `X-Operator-Key` / Bearer token on all operator endpoints.

### BUG-018: Outbox Sandbox Filename Collision Overwrites Dispatch Artifacts within Same Second — Fixed 2026-08-31
- **File:** `app/channels.py:56, 152`
- **Resolution:** Used millisecond timestamp `msg_id` in outbox sandbox filenames.

### BUG-019: Debtor Language Set to `None` Overrides Decision Language and Defaults to English — Fixed 2026-08-31
- **File:** `app/messages.py:55-57`
- **Resolution:** Coalesced `str(debtor.get("language") or decision.language).lower()`.

### BUG-020: Live Resend API Exception Swallowed in `DispatchResult` — Fixed 2026-08-31
- **File:** `app/channels.py:100-122`
- **Resolution:** Recorded and returned `error=last_error` in `DispatchResult`.

### BUG-021: Sanitized Draft Message Retains `dark_pattern_clean=False` — Fixed 2026-08-31
- **File:** `app/messages.py:270-294`
- **Resolution:** Set `is_clean = True` after sanitizing copy before initializing `DraftedMessage`.

### BUG-022: Operator UI Fails to Display Message Body, Subject, and Reasoning for Human Review — Fixed 2026-08-31
- **File:** `app/templates/operator.html:37-69`
- **Resolution:** Added expandable preview accordion displaying reasoning, subject, and draft message body.

### BUG-023: Review Queue Silently Overwrites Prior Items When Multiple Actions Are Queued for the Same Debtor — Fixed 2026-08-31
- **File:** `app/operator.py:70`
- **Resolution:** Stored a list/FIFO queue of review items per debtor in `_REVIEW_QUEUE`.

### BUG-024: Unbounded `reason` Field in `ReviewActionRequest` Allows Arbitrarily Large Payloads into the Audit Log — Fixed 2026-08-31
- **File:** `app/server.py:648-651`
- **Resolution:** Added `Field(max_length=2000)` constraint to `ReviewActionRequest.reason`.

### BUG-025: Test Fidelity Gap: `test_messages.py` Ineligible Trader Test Passes `Strategy.REQUEST_PAYMENT` Instead of `Strategy.ESCALATE` — Fixed 2026-08-31
- **File:** `test_messages.py:63-82`
- **Resolution:** Updated test to use `Strategy.ESCALATE` with `ineligible_trader`.

### BUG-026: `test_statute.py` Mutates Global `INVOICES` Dictionary In-Place Without Cleanup — Fixed 2026-08-31
- **File:** `test_statute.py:361-378`
- **Resolution:** Added `setUp`/`tearDown` deepcopy restoration on `INVOICES`.

### BUG-027: `test_operator.py` Does Not Clean Up `_REVIEW_QUEUE` and Lacks Tests for Rejection and Error Paths — Fixed 2026-08-31
- **File:** `test_operator.py:11-70`
- **Resolution:** Added comprehensive unit tests covering rejection, 404, kill switch blocking, and clean state teardown.

### BUG-028: `GET /api/operator/export` Missing `Content-Disposition` Attachment Header — Fixed 2026-08-31
- **File:** `app/server.py:702-704`
- **Resolution:** Added `Content-Disposition: attachment; filename="audit_events.{format}"` response header.

### BUG-029: Unhandled `TypeError` on Null `days_overdue` in Statutory Notice Evaluation — Fixed 2026-08-31
- **File:** `app/messages.py:181`
- **Resolution:** Coalesced `days_overdue = primary_invoice.get("days_overdue") or 0` before checking `> 15`.

### BUG-030: Unhandled `TypeError` on Null `days_overdue` in Dispute Fallback Delivery Date Calculation — Fixed 2026-08-31
- **File:** `app/server.py:601`
- **Resolution:** Coalesced `days_overdue = invoice.get("days_overdue") or 15` before timedelta computation.

### BUG-031: Non-Constant-Time Operator Secret Key Comparison Allows Timing Side-Channel Attacks — Fixed 2026-08-31
- **File:** `app/server.py:698`
- **Resolution:** Replaced direct string inequality with `hmac.compare_digest(key, expected)`.

### BUG-032: Duplicate HTML Element IDs in Operator Dashboard on Multiple Queued Items for Same Debtor — Fixed 2026-08-31
- **File:** `app/templates/operator.html:56, 69`
- **Resolution:** Added loop index to row DOM IDs (`row-{{ item.debtor_id }}-{{ loop.index }}`) and bound event listeners to remove closest row elements.

### BUG-033: Unstripped Punctuation in Debtor Name Generates Malformed Synthetic Fallback Email — Fixed 2026-08-31
- **File:** `app/pipeline.py:66-67`
- **Resolution:** Sanitized name string with `re.sub(r"[^a-z0-9]", "", debtor_name_str.lower())`.
