"""Razorpay order creation and signature verification.

Two different secrets are in play here and confusing them fails silently in a way that
looks like "payments randomly stop working":

  - KEY_SECRET signs the checkout callback, over the string "order_id|payment_id".
  - WEBHOOK_SECRET signs the webhook, over the raw request body. It is configured
    separately in the Razorpay dashboard and is not derivable from the key pair.
"""

import hashlib
import hmac

import razorpay

from app import config

# Razorpay rejects orders below this. Validating here gives a 400 instead of a 500.
MIN_AMOUNT_PAISE = 100

_client = razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))


def create_order(amount_paise: int, receipt: str, notes: dict[str, str] | None = None) -> dict:
    """Create a Razorpay order. `amount_paise` is an integer count of paise, not rupees."""
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool):
        raise ValueError(f"amount_paise must be an int, got {type(amount_paise).__name__}")
    if amount_paise < MIN_AMOUNT_PAISE:
        raise ValueError(f"amount_paise must be >= {MIN_AMOUNT_PAISE}, got {amount_paise}")

    return _client.order.create(
        {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt,
            "notes": notes or {},
        }
    )


def _signature_matches(message: bytes, secret: str, provided: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    # compare_digest rather than ==, so a wrong signature cannot be reconstructed
    # byte by byte from response timing.
    return hmac.compare_digest(expected, provided)


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Verify the checkout success callback."""
    if not config.RAZORPAY_KEY_SECRET:
        raise RuntimeError(
            "RAZORPAY_KEY_SECRET is not set. Copy .env.example to .env and fill it in."
        )
    if not (order_id and payment_id and signature):
        return False
    message = f"{order_id}|{payment_id}".encode()
    return _signature_matches(message, config.RAZORPAY_KEY_SECRET, signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Verify a webhook delivery.

    `raw_body` must be the exact bytes received. Re-serialising parsed JSON changes key
    order and whitespace, which changes the digest, and every webhook then reads as
    forged.
    """
    if not config.RAZORPAY_WEBHOOK_SECRET:
        raise RuntimeError(
            "RAZORPAY_WEBHOOK_SECRET is not set. Create a webhook in the Razorpay "
            "dashboard (Settings > Webhooks), copy its secret into .env, and restart."
        )
    if not signature:
        return False
    return _signature_matches(raw_body, config.RAZORPAY_WEBHOOK_SECRET, signature)
