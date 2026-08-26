"""Environment configuration.

Razorpay credentials are required at import because nothing in this service works
without them. The webhook secret and the model key are checked at their point of use
instead, so the checkout path stays runnable before those are provisioned.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


RAZORPAY_KEY_ID = _required("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = _required("RAZORPAY_KEY_SECRET")

# Deferred: set in the Razorpay dashboard, not derivable from the key pair.
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.6-flash").strip()

# Measured Aug 26, structured-output calls against this key:
#   gemini-3.7-flash  0/8 succeeded, every call 504 DEADLINE_EXCEEDED. A 120s timeout
#                     also 504'd at 122s, so it is unavailable rather than slow.
#   gemini-3.6-flash  3/3 succeeded, median 7.4s   <- primary
#   gemini-3.5-flash  3/3 succeeded, median 6.5s
#   gemini-flash-latest  answered, then 503'd once under load
#
# Observed Aug 27 during the Day 2 decision batch (audit/events.jsonl, 17 failovers and
# one exhaustion): the primary gemini-3.6-flash served ZERO calls, returning 429 or 504
# on every attempt, and gemini-3.5-flash-lite served roughly half the decisions. So the
# Aug 26 medians above describe a quieter key than the one in use.
#
# NOT YET MEASURED: gemini-3.5-flash-lite and gemini-3.1-flash-lite. They were added for
# 503 capacity headroom, but the "all models that answered returned the same verdict"
# parity check was only ever run across the 3.7/3.6/3.5 tier, never against the lite tier.
# A lite model is materially weaker, so until that parity is re-measured, treat any number
# produced while a lite model was serving as provisional. Re-measure before the demo.
#
# gemini-flash-latest stays last on purpose: it is the only floating alias in the chain,
# so it is the one entry that survives a pinned version being retired.
LLM_FALLBACK_MODELS = [
    name.strip()
    for name in os.getenv(
        "LLM_FALLBACK_MODELS",
        "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-latest",
    ).split(",")
    if name.strip()
]

# Milliseconds, per attempt. Measured: the SDK default can hang for minutes on a 503.
# The model chain is the redundancy, so a single attempt that fails fast beats a slow
# in-place retry.
LLM_TIMEOUT_MS = int(os.getenv("LLM_TIMEOUT_MS", "20000"))

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
