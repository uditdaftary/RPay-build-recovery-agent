"""Checks for the signature path. Run with: python test_signatures.py

Signature verification is the one place in this service where a bug silently converts
"anyone can forge a settlement" into "payments appear to work". These assertions are
deliberately blunt.

The two known-answer vectors were computed from stdlib hmac against the algorithm
Razorpay documents, independently of app code, so they catch a change in the algorithm
rather than agreeing with whatever the code happens to do.
"""

import hashlib
import hmac
import json

from app import config, razorpay_gateway

PAYMENT_SECRET = "test_secret_do_not_use"
WEBHOOK_SECRET = "whsec_test_do_not_use"

KNOWN_PAYMENT_SIGNATURE = "792681329348dabc338741acd766f93830b269231467f7862059b93657e4a7ac"
KNOWN_WEBHOOK_SIGNATURE = "b00f66913d76d2e4f32ca7dfaf83a1427e3f41ce4134f169c8830d9213afed95"


def sign(message: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def test_payment_signature() -> None:
    config.RAZORPAY_KEY_SECRET = PAYMENT_SECRET
    razorpay_gateway.config.RAZORPAY_KEY_SECRET = PAYMENT_SECRET

    order_id, payment_id = "order_ABC123", "pay_XYZ789"
    good = sign(f"{order_id}|{payment_id}".encode(), PAYMENT_SECRET)

    assert good == KNOWN_PAYMENT_SIGNATURE, "algorithm drifted from the documented one"
    assert razorpay_gateway.verify_payment_signature(order_id, payment_id, good)

    # A payment id swapped after signing must not verify.
    assert not razorpay_gateway.verify_payment_signature(order_id, "pay_TAMPERED", good)
    # An order id swapped after signing must not verify.
    assert not razorpay_gateway.verify_payment_signature("order_TAMPERED", payment_id, good)
    # A signature made with a different secret must not verify.
    forged = sign(f"{order_id}|{payment_id}".encode(), "attacker_secret")
    assert not razorpay_gateway.verify_payment_signature(order_id, payment_id, forged)
    # Missing pieces are a rejection, never an exception.
    assert not razorpay_gateway.verify_payment_signature(order_id, payment_id, "")
    assert not razorpay_gateway.verify_payment_signature("", payment_id, good)

    print("ok  payment signature")


def test_webhook_signature() -> None:
    razorpay_gateway.config.RAZORPAY_WEBHOOK_SECRET = WEBHOOK_SECRET

    raw = b'{"event":"payment.captured"}'
    good = sign(raw, WEBHOOK_SECRET)

    assert good == KNOWN_WEBHOOK_SIGNATURE, "algorithm drifted from the documented one"
    assert razorpay_gateway.verify_webhook_signature(raw, good)

    # The webhook secret is not the key secret. Signing with the wrong one must fail.
    assert not razorpay_gateway.verify_webhook_signature(raw, sign(raw, PAYMENT_SECRET))

    # The classic bug: verifying a re-serialised body instead of the received bytes.
    # json.dumps adds spaces, the digest changes, and every real webhook reads as forged.
    reserialised = json.dumps(json.loads(raw)).encode()
    assert reserialised != raw, "test is meaningless if these match"
    assert not razorpay_gateway.verify_webhook_signature(reserialised, good)

    assert not razorpay_gateway.verify_webhook_signature(raw, "")

    print("ok  webhook signature")


def test_webhook_secret_required() -> None:
    razorpay_gateway.config.RAZORPAY_WEBHOOK_SECRET = ""
    try:
        razorpay_gateway.verify_webhook_signature(b"{}", "abc")
    except RuntimeError as exc:
        assert "RAZORPAY_WEBHOOK_SECRET" in str(exc)
        print("ok  missing webhook secret fails loudly")
        return
    raise AssertionError("an unset webhook secret must raise, not silently accept or reject")


def test_amount_validation() -> None:
    for bad in (0, 99, -500):
        try:
            razorpay_gateway.create_order(bad, "INV-TEST")
        except ValueError:
            continue
        raise AssertionError(f"amount {bad} paise should have been rejected")

    # bool is an int subclass in Python, and True would become 1 paise.
    try:
        razorpay_gateway.create_order(True, "INV-TEST")  # type: ignore[arg-type]
    except ValueError:
        pass
    else:
        raise AssertionError("bool amount should have been rejected")

    print("ok  amount validation")


def test_inr_formatting() -> None:
    from app.server import format_inr

    assert format_inr(100) == "1.00"
    assert format_inr(99) == "0.99"
    assert format_inr(380000_00) == "3,80,000.00"
    assert format_inr(47500_00) == "47,500.00"
    assert format_inr(12345678_90) == "1,23,45,678.90"
    print("ok  indian digit grouping")


def test_health_endpoint() -> None:
    from app.server import health

    res = health()
    assert res.get("ok") is True
    assert "key_configured" in res
    assert "razorpay_key_id" not in res
    print("ok  health endpoint sanitisation")


if __name__ == "__main__":
    test_payment_signature()
    test_webhook_signature()
    test_webhook_secret_required()
    test_amount_validation()
    test_inr_formatting()
    test_health_endpoint()
    print("\nall checks passed")

