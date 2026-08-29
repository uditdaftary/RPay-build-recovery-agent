"""Dispute taxonomy and statutory clock recomputation under MSMED Section 15.

Under MSMED Act, 2006 (Section 15):
- Deemed Acceptance: If no written objection is raised within 15 calendar days of
  physical delivery of goods or rendering of services, acceptance date is deemed to be
  the delivery date.
- Objection Rule: If a written objection regarding defect or deficiency is communicated
  within 15 calendar days from delivery, the date of acceptance is moved to the date on
  which the supplier removes/resolves the objection.
- A dispute is not merely a pause in chasing: it moves the legal acceptance date,
  the statutory due date (capped at 45 days), and the Section 16 interest clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from app.ledger import (
    STATUTORY_WINDOW_DEFAULT_DAYS,
    STATUTORY_WINDOW_WRITTEN_DAYS,
)


class DisputeCategory(StrEnum):
    GOODS_SERVICES = "GOODS_SERVICES"
    INVOICE_MISMATCH = "INVOICE_MISMATCH"
    DUPLICATE = "DUPLICATE"
    TAX_GST = "TAX_GST"
    ALREADY_PAID = "ALREADY_PAID"
    WRONG_RECIPIENT = "WRONG_RECIPIENT"
    CONTRACTUAL = "CONTRACTUAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DisputeRecord:
    invoice_id: str
    category: DisputeCategory
    reason: str
    raised_date: date
    resolved_date: date | None = None
    objection_within_15d: bool = False


def classify_dispute_reason(reason_text: str | None) -> DisputeCategory:
    """Classify free-form dispute description into standard taxonomy."""
    if not reason_text or not reason_text.strip():
        return DisputeCategory.UNKNOWN

    text = reason_text.lower().strip()

    if any(k in text for k in ("neft", "utr", "already settled", "already paid", "paid on", "bank transfer")):
        return DisputeCategory.ALREADY_PAID

    if any(k in text for k in ("duplicate", "duplicate inv", "already invoiced", "double billed")):
        return DisputeCategory.DUPLICATE

    if any(k in text for k in ("gst", "tax rate", "tax mismatch", "tds", "hsn", "tax percent")):
        return DisputeCategory.TAX_GST

    if any(k in text for k in ("wrong recipient", "wrong entity", "wrong company", "wrong subsidiary", "subsidiary entity", "not our company")):
        return DisputeCategory.WRONG_RECIPIENT

    if any(k in text for k in ("rate on the invoice", "purchase order", "po mismatch", "po rate", "unit rate", "pricing discrepancy", "rate mismatch")):
        return DisputeCategory.INVOICE_MISMATCH

    if any(k in text for k in ("contract", "sla", "milestone", "clause", "terms of agreement")):
        return DisputeCategory.CONTRACTUAL

    if any(k in text for k in ("short delivery", "units received", "damaged", "quality", "defective", "goods", "service", "shortage")):
        return DisputeCategory.GOODS_SERVICES

    return DisputeCategory.UNKNOWN


def recompute_statutory_dates_on_dispute(
    delivery_date: date,
    written_agreement: bool,
    objection_date: date,
    resolution_date: date | None = None,
) -> tuple[date | None, date | None, date | None]:
    """Recompute MSMED statutory acceptance, due date, and appointed day upon dispute.

    Returns:
        (acceptance_date, statutory_due_date, appointed_day)
        If a valid objection was raised within 15 days but remains unresolved,
        returns (None, None, None) because statutory clock has not started.
    """
    days_from_delivery = (objection_date - delivery_date).days
    window = (
        STATUTORY_WINDOW_WRITTEN_DAYS if written_agreement else STATUTORY_WINDOW_DEFAULT_DAYS
    )

    if days_from_delivery <= 15:
        # Valid objection within 15 days of delivery under Section 15
        if resolution_date is None:
            # Active unresolved dispute: acceptance has not occurred
            return None, None, None

        acceptance = resolution_date
        statutory_due = acceptance + timedelta(days=window)
        appointed = acceptance + timedelta(days=16) if not written_agreement else statutory_due + timedelta(days=1)
        return acceptance, statutory_due, appointed

    # Objection was raised after 15 days: deemed acceptance on delivery date stands
    acceptance = delivery_date
    statutory_due = acceptance + timedelta(days=window)
    appointed = acceptance + timedelta(days=16) if not written_agreement else statutory_due + timedelta(days=1)
    return acceptance, statutory_due, appointed
