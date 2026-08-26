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
# All models that answered returned the same verdict, so the chain affects whether a
# decision arrives, not what it is. Tried in order on a retryable error.
LLM_FALLBACK_MODELS = [
    name.strip()
    for name in os.getenv("LLM_FALLBACK_MODELS", "gemini-3.5-flash,gemini-flash-latest").split(",")
    if name.strip()
]

# Milliseconds, per attempt. Measured: the SDK default can hang for minutes on a 503.
# The model chain is the redundancy, so a single attempt that fails fast beats a slow
# in-place retry.
LLM_TIMEOUT_MS = int(os.getenv("LLM_TIMEOUT_MS", "20000"))

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
