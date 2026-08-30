"""B2B Standing Instruction & Mandate Retry Sequencer Stub.

Architecture Context:
---------------------
Why a lightweight stub rather than a full day's build?
In B2B trade commerce (agencies, distributors, manufacturers), large transactions
(₹2L - ₹50L+) are executed as invoiced trade credit settled via NEFT/RTGS or dynamic
Razorpay checkout orders, not auto-debited recurring consumer mandates.
Recurring e-NACH/UPI Standing Instructions apply to a minority of SaaS retainer models.
Building an exhaustive mandate retry state machine would consume engineering budget
on fully simulated webhook loops without watchable video assets. We provide this
calibrated retry planner to model the mandate failure lifecycle and route expired
tokens back to fresh digital authorization links.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from app import audit


class MandateFailureCode(StrEnum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"


@dataclass(frozen=True)
class MandateRetryPlan:
    mandate_id: str
    failure_code: MandateFailureCode
    retry_dates: list[date]
    strategy_notes: str


def plan_mandate_retries(
    mandate_id: str,
    failure_code: MandateFailureCode,
    failure_date: date,
) -> MandateRetryPlan:
    """Compute optimal retry schedule based on banking failure root causes."""
    retry_dates: list[date] = []
    notes: str = ""

    if failure_code == MandateFailureCode.INSUFFICIENT_FUNDS:
        # 3-step cadence aligned with corporate cashflow and liquidity replenishment
        d1 = failure_date + timedelta(days=3)
        d2 = d1 + timedelta(days=5)
        d3 = d2 + timedelta(days=7)
        retry_dates = [d1, d2, d3]
        notes = "Insufficient funds: scheduled 3-step retry (+3d, +8d, +15d) to match cashflow cycle."

    elif failure_code == MandateFailureCode.LIMIT_EXCEEDED:
        d1 = failure_date + timedelta(days=1)
        d2 = failure_date + timedelta(days=5)
        retry_dates = [d1, d2]
        notes = "Bank debit limit exceeded: scheduled retries after daily/monthly limit reset."

    elif failure_code == MandateFailureCode.MANDATE_EXPIRED:
        retry_dates = []
        notes = "Mandate expired: automated retry suppressed. Routed to debtor for fresh e-mandate re-authorization."

    elif failure_code == MandateFailureCode.TECHNICAL_ERROR:
        retry_dates = [failure_date + timedelta(days=1)]
        notes = "Bank gateway technical failure: transient retry scheduled within 24 hours."

    audit.record(
        "mandate.retry_planned",
        mandate_id=mandate_id,
        failure_code=failure_code.value,
        retry_count=len(retry_dates),
        first_retry=retry_dates[0].isoformat() if retry_dates else None,
    )

    return MandateRetryPlan(
        mandate_id=mandate_id,
        failure_code=failure_code,
        retry_dates=retry_dates,
        strategy_notes=notes,
    )
