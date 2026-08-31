"""Durable state, when a durable place to put it exists.

The audit log is not a record of decisions, it is an input to them: `contact_history`
folds it on every decision, so a lost `settlement.confirmed` row resumes chasing a debtor
who has already paid. On a single machine an append-only file is the right answer and
stays the default. On serverless there is no durable filesystem and no shared memory
between invocations, so the same file loses settlements, forgets promises, and leaves the
kill switch set on one instance and clear on the next.

`DATABASE_URL` is the switch. Set, this module owns the four pieces of state that have to
outlive a request; unset, every caller keeps the filesystem and in-memory behaviour it had
before, which is what the test suite and the seeded benchmark run against.

Nothing here is on the decision path in file mode, and the benchmark forces file mode
regardless (see `run_experiment.isolated_audit_log`), so a `/results` run stays a pure
function of its seed and costs no round trips.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# `.env` is loaded here rather than inherited from `app.config`, which validates Razorpay
# credentials at import and raises without them - this module has to be usable before those
# are provisioned. Without it `DATABASE_URL` is invisible to any entrypoint that does not
# import `app.config` first, so a bare `from app import store` silently reported no durable
# backend while one was configured.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    event   text NOT NULL,
    payload jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_debtor_idx
    ON audit_events ((payload->>'debtor_id'));

CREATE TABLE IF NOT EXISTS invoice_runtime (
    invoice_id text PRIMARY KEY,
    state      jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_state (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    id        bigserial PRIMARY KEY,
    debtor_id text NOT NULL,
    item      jsonb NOT NULL,
    queued_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_queue_debtor_idx ON review_queue (debtor_id, id);
"""

_pool_lock = threading.Lock()
_pool: Any = None
_schema_ready = False


def database_url() -> str:
    """The configured Postgres DSN, or empty string for filesystem mode.

    Read at call time rather than import time: Vercel injects environment variables into
    the function runtime, and a module-level constant captured at cold start in a process
    that imported this before the environment was populated would pin the wrong mode.
    """
    return os.getenv("DATABASE_URL", "").strip()


def is_enabled() -> bool:
    """Whether durable state is available. False means every caller keeps its old path."""
    return bool(database_url())


def _connect() -> Any:
    """One pooled connection. Import is lazy so psycopg stays an optional dependency."""
    global _pool, _schema_ready
    from psycopg_pool import ConnectionPool

    dsn = database_url()
    if not dsn:
        # Fail immediately rather than letting psycopg fall back to a default local
        # connection and time out after 30 seconds. On a serverless function that hang is
        # the whole request budget, and the cause - an unset variable - is not in the
        # timeout message.
        raise RuntimeError(
            "DATABASE_URL is not set, so there is no durable store to reach. Callers must "
            "check store.is_enabled() before using it."
        )

    with _pool_lock:
        if _pool is None:
            # Small: a serverless instance serves few concurrent requests and a large pool
            # against a connection-limited Postgres is how a deploy exhausts the database
            # rather than the function.
            _pool = ConnectionPool(dsn, min_size=0, max_size=4, open=True)
            # Closed deterministically rather than by the garbage collector: the pool's
            # own finaliser joins its worker threads, and doing that during interpreter
            # shutdown raises PythonFinalizationError on every exit.
            atexit.register(reset_for_tests)
        if not _schema_ready:
            with _pool.connection() as conn:
                conn.execute(_SCHEMA)
            _schema_ready = True
    return _pool


def reset_for_tests() -> None:
    """Drop the cached pool so a test can point the module at a different database."""
    global _pool, _schema_ready
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _schema_ready = False


# --------------------------------------------------------------------------- audit log


def append_event(entry: dict[str, Any]) -> None:
    """Append one audit row. The caller has already stamped `ts` and `event`."""
    pool = _connect()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO audit_events (event, payload) VALUES (%s, %s)",
            (str(entry.get("event", "")), json.dumps(entry, ensure_ascii=False, default=str)),
        )


def read_events() -> list[dict[str, Any]]:
    """Every audit row in insertion order.

    Ordered by the serial id, not by `ts`: two rows written in the same millisecond have
    the same stamp, and `contact_history` reads later rows as superseding earlier ones.
    """
    pool = _connect()
    with pool.connection() as conn:
        rows = conn.execute("SELECT payload FROM audit_events ORDER BY id").fetchall()
    return [row[0] for row in rows]


# ------------------------------------------------------------------- invoice lifecycle


def load_invoice_runtime() -> dict[str, dict[str, Any]]:
    """The mutable half of every invoice, keyed by invoice id.

    Only the runtime lifecycle lives here. The ledger-derived fields stay immutable in
    memory because `data/ledger.json` is the experiment's seed and has to stay
    byte-identical.
    """
    pool = _connect()
    with pool.connection() as conn:
        rows = conn.execute("SELECT invoice_id, state FROM invoice_runtime").fetchall()
    return {row[0]: row[1] for row in rows}


def save_invoice_runtime(invoice_id: str, state: dict[str, Any]) -> None:
    pool = _connect()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO invoice_runtime (invoice_id, state) VALUES (%s, %s) "
            "ON CONFLICT (invoice_id) DO UPDATE SET state = EXCLUDED.state",
            (invoice_id, json.dumps(state, default=str)),
        )


def claim_payment(invoice_id: str, payment_id: str) -> bool:
    """Register a capture, returning False if this payment was already applied.

    The webhook's replay guard. Razorpay redelivers, and a redelivered *partial* leaves the
    status PARTIALLY_PAID, so a status check would let it through and double-credit. In
    filesystem mode a process lock is enough; across lambdas only the database can decide,
    so the claim is a conditional insert and the row itself is the lock.
    """
    pool = _connect()
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO audit_events (event, payload) "
            "SELECT %s, %s WHERE NOT EXISTS ("
            "  SELECT 1 FROM audit_events "
            "  WHERE event = %s AND payload->>'invoice_id' = %s AND payload->>'payment_id' = %s"
            ") RETURNING id",
            (
                "settlement.payment_claimed",
                json.dumps({"invoice_id": invoice_id, "payment_id": payment_id}),
                "settlement.payment_claimed",
                invoice_id,
                payment_id,
            ),
        ).fetchone()
    return row is not None


# -------------------------------------------------------------------- operator controls


def get_kill_switch() -> bool:
    pool = _connect()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM operator_state WHERE key = 'kill_switch'"
        ).fetchone()
    return bool(row[0]) if row else False


def set_kill_switch(active: bool) -> bool:
    pool = _connect()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO operator_state (key, value) VALUES ('kill_switch', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (json.dumps(bool(active)),),
        )
    return bool(active)


def enqueue_review(debtor_id: str, item: dict[str, Any]) -> None:
    pool = _connect()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO review_queue (debtor_id, item) VALUES (%s, %s)",
            (debtor_id, json.dumps(item, default=str)),
        )


def list_review_queue() -> list[dict[str, Any]]:
    pool = _connect()
    with pool.connection() as conn:
        rows = conn.execute("SELECT item FROM review_queue ORDER BY id").fetchall()
    return [row[0] for row in rows]


def pop_review_item(debtor_id: str) -> dict[str, Any] | None:
    """Take the oldest queued action for a debtor, or None.

    `FOR UPDATE SKIP LOCKED` so two operators clicking Approve at the same moment take two
    different items rather than dispatching the same notice twice.
    """
    pool = _connect()
    with pool.connection() as conn:
        row = conn.execute(
            "DELETE FROM review_queue WHERE id = ("
            "  SELECT id FROM review_queue WHERE debtor_id = %s "
            "  ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
            ") RETURNING item",
            (debtor_id,),
        ).fetchone()
    return row[0] if row else None


def requeue_review_item(debtor_id: str, item: dict[str, Any]) -> None:
    """Put a popped item back at the head after a failed dispatch.

    A negative id keeps it ahead of everything already queued without renumbering the
    sequence, which preserves the FIFO the operator sees.
    """
    pool = _connect()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO review_queue (id, debtor_id, item) "
            "VALUES ((SELECT COALESCE(MIN(id), 0) - 1 FROM review_queue), %s, %s)",
            (debtor_id, json.dumps(item, default=str)),
        )
