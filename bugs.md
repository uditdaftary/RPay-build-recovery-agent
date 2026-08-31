# Bug Report — recovery-agent PR #9 — 2026-09-01

## Summary
- Critical: 0 open, 9 fixed
- Intermediate: 6 open, 16 fixed
- Normal: 11 open, 23 fixed
- Total Open Findings: 17
- Total Resolved Findings: 48 (100% of BUG-001 through BUG-048 verified resolved)

---

## 🔴 Critical
*(All 9 previous Critical findings verified resolved; 0 new open critical flaws detected.)*

---

## 🟡 Intermediate

### BUG-049: Non-Atomic `claim_payment` Race Condition in PostgreSQL Default Read Committed Isolation
- **File:** `app/store.py:193-207`
- **Issue:** `claim_payment` uses `INSERT INTO audit_events ... SELECT ... WHERE NOT EXISTS (...)` to atomically claim a payment against replay attacks across distributed serverless instances. However, under PostgreSQL's default `READ COMMITTED` transaction isolation level, two concurrent webhook deliveries for the same payment can execute the `WHERE NOT EXISTS` subquery simultaneously before either transaction commits, causing both inserts to succeed.
- **Trigger:** Simultaneous concurrent webhook redelivery / duplicate callbacks from Razorpay for the same `(invoice_id, payment_id)`.
- **Impact:** Both concurrent requests believe they acquired the unique claim, resulting in duplicate payment processing or double-crediting of partial settlements in distributed environments.
- **Suggested Fix:** Add a `UNIQUE (payload->>'invoice_id', payload->>'payment_id')` partial index on `settlement.payment_claimed` events in the PostgreSQL schema, or use `INSERT ... ON CONFLICT DO NOTHING RETURNING id`.
- **Status:** Open

### BUG-050: Concurrent `requeue_review_item` Primary Key Collision and Inverted FIFO Queue Ordering
- **File:** `app/store.py:276-278`
- **Issue:** `requeue_review_item` computes a new row ID via `(SELECT COALESCE(MIN(id), 0) - 1 FROM review_queue)` to place failed dispatches at the head of the queue. If two worker threads requeue failed items concurrently, both subqueries evaluate to the same negative integer, triggering a Postgres Primary Key `unique_violation` error. Additionally, sequential requeuing assigns increasingly negative numbers, causing `ORDER BY id LIMIT 1` in `pop_review_item` to pop the most recently requeued item first (LIFO order) rather than preserving original queue timestamps.
- **Trigger:** Consecutive or concurrent dispatch errors during human operator batch approval when PostgreSQL storage is enabled.
- **Impact:** Server crashes with database unique constraint violation on concurrent requeuing, and queue ordering becomes LIFO for consecutively requeued items.
- **Suggested Fix:** Utilize a dedicated priority column or timestamp-based ordering (`ORDER BY priority DESC, queued_at ASC`) instead of synthesizing negative primary keys.
- **Status:** Open

### BUG-051: Unconditional `load_dotenv` in `store.py` Pollutes Non-Pytest Unit Test Runners with Live Remote `DATABASE_URL`
- **File:** `app/store.py:38` & `conftest.py:20`
- **Issue:** `app/store.py` unconditionally invokes `load_dotenv(Path(__file__).resolve().parent.parent / ".env")` at top-level import. While `conftest.py` contains a pytest session fixture to clear `DATABASE_URL`, standalone test runners or standard library `unittest` discover runs (`python -m unittest discover`) do not invoke pytest fixtures.
- **Trigger:** Running unit test suites using standard Python `unittest` runner or third-party test runners on a developer environment with a populated `.env`.
- **Impact:** Unit tests silently connect to live remote PostgreSQL databases, potentially mutating live tables and slowing down test suites.
- **Suggested Fix:** Guard `load_dotenv` with a check such as `if "PYTEST_CURRENT_TEST" not in os.environ and "UNITTEST" not in os.environ:` or avoid automatic dotenv loading during test imports.
- **Status:** Open

### BUG-052: Promise Window Accumulator Logic Error in `record_promise` Compounds Super-Linearly Across Multiple Invoices
- **File:** `app/server.py:610-623`
- **Issue:** `record_promise` calculates existing promise windows for a debtor by summing across all invoices (`opened = sum(other.get("promise_windows", 0) for other in INVOICES.values() if other["debtor_id"] == invoice["debtor_id"])`) and then assigns `invoice["promise_windows"] = opened + 1`. When a debtor holds multiple invoices and records promises across them, storing the aggregated debtor sum into a single invoice's counter causes the next sum evaluation to compound super-linearly.
- **Trigger:** A debtor with two or more invoices recording promises alternately on each invoice.
- **Impact:** The debtor prematurely exhausts their `MAX_PROMISE_WINDOWS_WITHOUT_SETTLEMENT` quota (e.g. after only 2 actual promises instead of 3), prematurely blocking valid payment commitments with HTTP 400.
- **Suggested Fix:** Increment only the specific target invoice's `promise_windows` by 1 (`invoice["promise_windows"] = invoice.get("promise_windows", 0) + 1`), or track promise windows in a dedicated debtor-level state dictionary.
- **Status:** Open

### BUG-053: Non-Thread-Safe Mutation and Eviction of Module-Global `_RESULTS_CACHE` in Multi-Threaded Server Runtime
- **File:** `app/server.py:927, 948-952, 1032-1035`
- **Issue:** `_RESULTS_CACHE` is an `OrderedDict` stored as a module-level global variable in `app/server.py`. In multi-threaded ASGI/WSGI server runtimes (e.g., Uvicorn with worker threads), concurrent calls to `/results` or `/api/results` read, insert, and evict (`popitem(last=False)`) without acquiring a thread lock / mutex.
- **Trigger:** High concurrency of requests with varying `seed` and `as_of` query parameters against `/results` or `/api/results`.
- **Impact:** Concurrent dictionary mutations lead to `KeyError`, `RuntimeError: dictionary changed size during iteration`, or corrupted cache state.
- **Suggested Fix:** Guard all access, mutations, and eviction of `_RESULTS_CACHE` with a threading mutex (`threading.Lock()`).
- **Status:** Open

### BUG-054: Google GenAI `response.text` Property Access Raises Uncaught `ValueError` on Safety-Blocked or Empty Candidates
- **File:** `app/llm.py:110-150`
- **Issue:** In the Google GenAI SDK (`google-genai`), `response.text` is a property that raises a `ValueError` when the response candidates are blocked by safety filters, empty, or finish with reasons other than standard stop. The `try/except` block in `complete_prompt` only catches `(errors.APIError, httpx.TimeoutException, httpx.TransportError)`.
- **Trigger:** Model returns a safety-blocked candidate or empty response structure during copy generation.
- **Impact:** Unhandled `ValueError` escapes and crashes the prompt execution immediately, bypassing the multi-model fallback chain and preventing fallback models from being attempted.
- **Suggested Fix:** Wrap `response.text` access in a `try/except (ValueError, AttributeError)` block or inspect `response.candidates[0].content` safely before accessing the `.text` property.
- **Status:** Open

---

## 🟢 Normal

### BUG-055: `approve_review_item` Records False Kill Switch Block and Returns HTTP 409 for Non-Existent Debtors
- **File:** `app/operator.py:135-139`
- **Issue:** When the master kill switch is engaged, `approve_review_item()` checks `if not get_review_queue(): return None`. If the review queue contains items for *other* debtors, this check passes and the function immediately records an `operator.approval_blocked_by_kill_switch` audit event and returns HTTP 409 for the requested `debtor_id`, even if that `debtor_id` does not exist in the queue.
- **Trigger:** Sending `POST /api/operator/approve` with an invalid or already processed `debtor_id` while the kill switch is active and the review queue is non-empty.
- **Impact:** Returns HTTP 409 Conflict instead of HTTP 404 Not Found, and pollutes the audit log with false kill switch block records for non-existent queue items.
- **Suggested Fix:** Verify that the specific requested `debtor_id` exists in the review queue before recording the audit event and returning HTTP 409.
- **Status:** Open

### BUG-056: `dispatch_email` References Unbound Variable `req_payload` in Exception Handler
- **File:** `app/channels.py:91-149`
- **Issue:** In `dispatch_email()`, `req_payload` is initialized inside the `try:` block after `import urllib.request`. If an exception occurs prior to `req_payload` assignment (e.g. during module import or dictionary preparation), the `except Exception as exc:` handler attempts to pass `payload=req_payload` to `DispatchResult`.
- **Trigger:** An exception raised prior to `req_payload` assignment in the `live` dispatch branch.
- **Impact:** The exception handler crashes with `UnboundLocalError: local variable 'req_payload' referenced before assignment`, masking the root cause exception and preventing `DispatchResult(success=False)` from being returned cleanly.
- **Suggested Fix:** Initialize `req_payload: dict[str, Any] | None = None` before the `try:` block.
- **Status:** Open

### BUG-057: Unvalidated `format` Query Parameter in `GET /api/operator/export` Injected Directly into `Content-Disposition` Header
- **File:** `app/server.py:850-859`
- **Issue:** In `export_audit_log`, the `format` query parameter is typed as a loose string (`format: str = "json"`). It is directly interpolated into the HTTP response header: `headers = {"Content-Disposition": f'attachment; filename="audit_events.{format.lower()}"'}` without validation against allowed values or header sanitization.
- **Trigger:** Requesting `GET /api/operator/export?format=json%0d%0aInjected-Header:%20value` or passing arbitrary string extensions.
- **Impact:** Header injection risks on certain HTTP proxies / ASGI servers, and generation of invalid attachment filenames for unsupported format parameters.
- **Suggested Fix:** Validate `format` with an Enum / Literal constraint (`format: Literal["json", "csv"] = "json"`), returning HTTP 400 Bad Request on invalid inputs.
- **Status:** Open

### BUG-058: `_load()` and `_load_file()` in `contacts.py` Fail with `AttributeError` on Non-Dictionary JSON Roots
- **File:** `app/contacts.py:68-94`
- **Issue:** `_load()` and `_load_file()` parse JSON from the environment or filesystem and return the raw output of `json.loads()`. If the JSON payload root is a list, string, or boolean (e.g., `[]` or `"null"`), `for_debtor()` crashes on `_load().get(...)` with `AttributeError`.
- **Trigger:** `DEBTOR_CONTACTS` environment variable or `contacts.json` containing a valid JSON non-object root (e.g. `[]`).
- **Impact:** Unhandled `AttributeError: 'list' object has no attribute 'get'` crashes debtor contact resolution.
- **Suggested Fix:** Explicitly type-check that `isinstance(data, dict)` before returning parsed JSON in `_load` and `_load_file`, returning `{}` otherwise.
- **Status:** Open

### BUG-059: `_statutory_due_date` Crashes with `TypeError` When Invoice Date Fields Are `datetime.date` Objects
- **File:** `app/messages.py:56-66`
- **Issue:** `_statutory_due_date()` calls `date.fromisoformat(raw_delivery)` and `date.fromisoformat(raw_due)`. If the invoice dictionary contains native `datetime.date` objects (e.g., from Pydantic models, ORM mappings, or test fixtures) rather than ISO format strings, `date.fromisoformat` raises a `TypeError`.
- **Trigger:** Calling message formatting functions with invoice dictionaries containing `datetime.date` instances in `delivery_date`, `invoice_date`, or `contractual_due_date`.
- **Impact:** Unhandled `TypeError: fromisoformat: argument must be str` crashes message generation.
- **Suggested Fix:** Use a helper that checks `isinstance(val, date)` and returns `val` directly, only calling `date.fromisoformat` if `isinstance(val, str)`.
- **Status:** Open

### BUG-060: Unescaped CSS Class Selector in Operator and Results Dashboard Throws `DOMException` on Special Characters in `debtor_id`
- **File:** `app/templates/operator.html:171, 203` & `app/templates/results.html:604`
- **Issue:** JavaScript functions use raw string concatenation in `document.querySelector(".row-" + debtorId)` and `document.querySelector(".toggle-btn-" + debtorId)`. If `debtor_id` contains CSS special characters like dots, colons, slashes, or hashes (e.g. `DEBTOR.1` or `D/001`), `querySelector` fails to parse the selector.
- **Trigger:** Loading debtor details or approving/rejecting actions for debtors with special characters in their identifiers.
- **Impact:** Throws client-side `DOMException: Failed to execute 'querySelector' on 'Document'`, causing UI buttons to become unresponsive.
- **Suggested Fix:** Use `CSS.escape(debtorId)` in query selectors or retrieve elements using `document.getElementById` and `data-` attributes.
- **Status:** Open

### BUG-061: WhatsApp Interactive Button Dispatch Lacks 1024-Character Payload Body Length Validation
- **File:** `app/channels.py:181-201`
- **Issue:** `dispatch_whatsapp()` constructs a WhatsApp Business API interactive button payload embedding `message.body` into `interactive.body.text`. The WhatsApp Cloud API enforces a strict maximum length of 1024 characters for interactive message bodies.
- **Trigger:** Drafting a lengthy Hinglish or English recovery notice with extensive invoice breakdowns exceeding 1024 characters.
- **Impact:** Live WhatsApp API rejects the message with HTTP 400 `(#100) Param interactive['body']['text'] has length exceeding 1024 characters`.
- **Suggested Fix:** Validate and truncate `message.body` to 1024 characters (or log a warning and fall back to standard text message) before dispatching interactive buttons.
- **Status:** Open

### BUG-062: Synchronous Database I/O and Mutex Acquisition in `async def razorpay_webhook` Blocks Main Asyncio Event Loop
- **File:** `app/server.py:511-545`
- **Issue:** `razorpay_webhook` is defined as an `async def` route handler on FastAPI/Starlette. Inside its execution path, `suppress_on_settlement` executes synchronous PostgreSQL database queries (via `store.claim_payment`) and file I/O operations directly on the main event loop thread without dispatching to a worker threadpool.
- **Trigger:** Receiving high volumes of inbound webhook events from Razorpay while database storage is active.
- **Impact:** Blocks the asyncio event loop thread during database socket I/O, degrading HTTP throughput and increasing response latency for all concurrent API requests.
- **Suggested Fix:** Offload synchronous database and file I/O operations to Starlette's worker threadpool using `await run_in_threadpool(...)` or define the endpoint as a standard synchronous `def` function.
- **Status:** Open

### BUG-063: Corrupted Row Error and Warning Messages in `audit.py` Cite Global Constant `EVENT_LOG` Instead of Context-Isolated Path
- **File:** `app/audit.py:197, 202`
- **Issue:** In `app/audit.py`, `read_all()` resolves the active log path via `event_log = get_event_log()`. However, the warning on line 197 and the exception on line 202 reference the global constant `EVENT_LOG` instead of the local variable `event_log`.
- **Trigger:** Encountering a truncated or corrupt audit line while running inside an isolated context (`with isolated_audit_log():`).
- **Impact:** Error messages and logs point to the default production file path (`audit/events.jsonl`) rather than the active temporary test/experiment file, confusing debugging and audit tracing.
- **Suggested Fix:** Replace occurrences of `EVENT_LOG` with `event_log` in `read_all()`.
- **Status:** Open

### BUG-064: Redundant Double JSON Serialization / Deserialization Cycle on Database Audit Appends
- **File:** `app/audit.py:114`
- **Issue:** In `record()`, when database mode is enabled, the entry is serialized to JSON and immediately deserialized back into a Python dictionary: `store.append_event(json.loads(json.dumps(entry, ensure_ascii=False, default=str)))`. `store.append_event()` then accepts this dictionary and serializes it once again into JSON for PostgreSQL insertion.
- **Trigger:** Every call to `audit.record()` under PostgreSQL store mode.
- **Impact:** Unnecessary CPU overhead and garbage collection pressure from performing double JSON encoding/decoding on every recorded audit event.
- **Suggested Fix:** Clean dictionary values directly or rely on `store.append_event` to perform the single required JSON serialization.
- **Status:** Open

### BUG-065: Channel Dispatch Entry Point Lacks Direct `is_kill_switch_active()` Defense-in-Depth Check
- **File:** `app/channels.py:228-245`
- **Issue:** `dispatch_message()` handles routing and dispatching to email, WhatsApp, and SMS channels. While the pipeline and operator consoles check the master kill switch, `dispatch_message` lacks a defense-in-depth verification of `is_kill_switch_active()`.
- **Trigger:** Directly calling `dispatch_message()` from an external script, background task, or unverified handler while the kill switch is active.
- **Impact:** Outbound messages can be dispatched even when the operator has engaged the master emergency kill switch.
- **Suggested Fix:** Add an immediate kill switch check at the beginning of `dispatch_message()`, returning `DispatchResult(success=False, error="Master kill switch is active")` if engaged.
- **Status:** Open

---

## ✅ Resolved

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
