"""MSMED Section 15/16 and Income Tax Section 43B(h) Statutory Engine.

Indian Statutory Delayed Payment Framework:
1. MSMED Act, 2006 (Section 15):
   - Payment clock capped at maximum 45 calendar days for written agreements,
     or 15 calendar days from delivery in the absence of a written agreement.
   - Deemed acceptance: If no written objection is raised within 15 days of delivery,
     acceptance is deemed on the delivery date.
   - Appointed day: Day immediately following the expiry of 15 days from acceptance.

2. MSMED Act, 2006 (Section 16):
   - Compound interest with monthly rests at exactly 3x RBI Bank Rate (default 20.25% p.a.).
   - Mandatory statutory override over any contrary commercial contract terms.
   - Integer paise precision with zero floating-point drift.

3. Income Tax Act, 1961 (Section 43B(h)):
   - Disallows deductions for sums owed to Micro and Small suppliers if unpaid beyond
     Section 15 limits by the close of the financial year (March 31).
   - Ineligible: Medium enterprises and Traders (even if Udyam registered).
   - Strict anti-dark-pattern guardrails prohibiting immediate penalty claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app import audit
from app.disputes import recompute_statutory_dates_on_dispute
from app.ledger import (
    STATUTORY_WINDOW_DEFAULT_DAYS,
    STATUTORY_WINDOW_WRITTEN_DAYS,
    Merchant,
    msmed_eligible,
)

DEFAULT_RBI_BANK_RATE_PCT: float = 6.75
STATUTORY_RATE_MULTIPLIER: int = 3

PROHIBITED_COPY_PATTERNS = (
    "immediately cancelled",
    "cancelled today",
    "immediate tax penalties",
    "penalties apply tomorrow",
    "immediate disallowance today",
    "deduction is cancelled today",
    "tax penalty is applied immediately",
)


@dataclass(frozen=True)
class StatutoryInterestResult:
    principal_paise: int
    due_date: date
    as_of_date: date
    days_overdue: int
    full_months: int
    residual_days: int
    annual_rate_pct: float
    compounded_principal_paise: int
    residual_interest_paise: int
    accrued_interest_paise: int
    total_payable_paise: int


@dataclass(frozen=True)
class Section43BHEvaluation:
    is_eligible: bool
    refusal_reason: str | None
    disallowance_fy: str | None = None
    disallowance_date: date | None = None
    compliant_notice_copy: str | None = None


def compute_statutory_dates(
    delivery_date: date,
    written_agreement: bool,
    objection_raised_within_15d: bool = False,
    resolution_date: date | None = None,
) -> tuple[date | None, date | None, date | None]:
    """Compute statutory acceptance date, due date, and appointed day under MSMED s.15."""
    if objection_raised_within_15d:
        return recompute_statutory_dates_on_dispute(
            delivery_date=delivery_date,
            written_agreement=written_agreement,
            objection_date=delivery_date + timedelta(days=1),
            resolution_date=resolution_date,
        )

    acceptance = delivery_date
    window = (
        STATUTORY_WINDOW_WRITTEN_DAYS if written_agreement else STATUTORY_WINDOW_DEFAULT_DAYS
    )
    statutory_due = acceptance + timedelta(days=window)
    appointed = acceptance + timedelta(days=16) if not written_agreement else statutory_due + timedelta(days=1)
    return acceptance, statutory_due, appointed


def calculate_section_16_interest(
    principal_paise: int,
    due_date: date,
    as_of: date,
    *,
    rbi_bank_rate_pct: float = DEFAULT_RBI_BANK_RATE_PCT,
    rest_interval_days: int = 30,
) -> StatutoryInterestResult:
    """Calculate compound interest with monthly rests under MSMED Section 16.

    Formula:
      - Statutory annual rate: r_annual = 3 * rbi_bank_rate_pct
      - Full monthly rest periods: M = days_overdue // rest_interval_days
      - Compounded amount after M months: A_M = P * (1 + r_annual/12)^M
      - Residual ongoing days: delta_d = days_overdue % rest_interval_days
      - Residual interest: Interest_residual = A_M * (r_annual * delta_d / 365)
      - Total amount payable: Total = A_M + Interest_residual
      - Accrued penal interest: Accrued = Total - P

    All monetary math is performed in exact Decimal and quantized to integer paise.
    """
    days_overdue = (as_of - due_date).days
    annual_rate_pct = rbi_bank_rate_pct * STATUTORY_RATE_MULTIPLIER

    if days_overdue <= 0 or principal_paise <= 0:
        return StatutoryInterestResult(
            principal_paise=principal_paise,
            due_date=due_date,
            as_of_date=as_of,
            days_overdue=max(0, days_overdue),
            full_months=0,
            residual_days=0,
            annual_rate_pct=annual_rate_pct,
            compounded_principal_paise=principal_paise,
            residual_interest_paise=0,
            accrued_interest_paise=0,
            total_payable_paise=principal_paise,
        )

    full_months = days_overdue // rest_interval_days
    residual_days = days_overdue % rest_interval_days

    p_dec = Decimal(principal_paise)
    r_annual_dec = Decimal(str(annual_rate_pct)) / Decimal("100")
    monthly_rate_dec = r_annual_dec / Decimal("12")

    # A_M = P * (1 + r/12)^M
    compounding_factor = (Decimal("1") + monthly_rate_dec) ** full_months
    a_m = p_dec * compounding_factor

    # Residual simple interest on A_M for delta_d days (statutory 365-day basis)
    residual_interest_dec = (
        a_m * (r_annual_dec * Decimal(residual_days) / Decimal("365"))
        if residual_days > 0
        else Decimal("0")
    )

    total_payable_dec = a_m + residual_interest_dec
    total_payable_paise = int(total_payable_dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    compounded_principal_paise = int(a_m.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    residual_interest_paise = int(residual_interest_dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    accrued_interest_paise = total_payable_paise - principal_paise

    return StatutoryInterestResult(
        principal_paise=principal_paise,
        due_date=due_date,
        as_of_date=as_of,
        days_overdue=days_overdue,
        full_months=full_months,
        residual_days=residual_days,
        annual_rate_pct=annual_rate_pct,
        compounded_principal_paise=compounded_principal_paise,
        residual_interest_paise=residual_interest_paise,
        accrued_interest_paise=accrued_interest_paise,
        total_payable_paise=total_payable_paise,
    )


def get_financial_year(d: date) -> str:
    """Return the Indian Financial Year string (e.g. '2026-27') for a given date."""
    if d.month >= 4:
        start_year = d.year
        end_year = d.year + 1
    else:
        start_year = d.year - 1
        end_year = d.year
    return f"{start_year}-{str(end_year)[2:]}"


def get_financial_year_end(d: date) -> date:
    """Return the financial year-end date (March 31) for a given date."""
    if d.month >= 4:
        return date(d.year + 1, 3, 31)
    return date(d.year, 3, 31)


def validate_section_43b_h_copy(copy_text: str) -> tuple[bool, str | None]:
    """Validate that Section 43B(h) communications comply with anti-dark-pattern rules."""
    text_lower = copy_text.lower()
    for pattern in PROHIBITED_COPY_PATTERNS:
        if pattern in text_lower:
            return False, f"Prohibited anti-dark-pattern phrase detected: '{pattern}'"
    return True, None


def evaluate_section_43b_h(
    merchant: Merchant | dict[str, Any],
    invoice: dict[str, Any],
    as_of: date,
) -> Section43BHEvaluation:
    """Evaluate applicability of Income Tax Section 43B(h) to an invoice."""
    if isinstance(merchant, dict):
        merchant_obj = Merchant(
            merchant_id=merchant["merchant_id"],
            name=merchant["name"],
            udyam_registered=merchant["udyam_registered"],
            udyam_category=merchant.get("udyam_category"),
            udyam_activity=merchant.get("udyam_activity"),
        )
    else:
        merchant_obj = merchant

    is_eligible, eligibility_reason = msmed_eligible(merchant_obj)
    if not is_eligible:
        return Section43BHEvaluation(
            is_eligible=False,
            refusal_reason=eligibility_reason,
        )

    fy = get_financial_year(as_of)
    fy_end = get_financial_year_end(as_of)
    notice_copy = (
        f"Invoice outstanding beyond the MSMED statutory period is subject to tax disallowance "
        f"under Section 43B(h) of the Income Tax Act for FY {fy} if unpaid at year-end."
    )

    return Section43BHEvaluation(
        is_eligible=True,
        refusal_reason=None,
        disallowance_fy=fy,
        disallowance_date=fy_end,
        compliant_notice_copy=notice_copy,
    )


def generate_section_43b_h_notice(
    merchant: Merchant | dict[str, Any],
    invoice: dict[str, Any],
    as_of: date,
) -> tuple[bool, str | None, str | None]:
    """Generate compliant notice copy for eligible merchants; suppress and log for ineligible.

    Returns:
        (is_eligible, compliant_copy, refusal_reason)
    """
    evaluation = evaluate_section_43b_h(merchant, invoice, as_of)
    if not evaluation.is_eligible:
        merchant_id = merchant.merchant_id if isinstance(merchant, Merchant) else merchant.get("merchant_id", "")
        debtor_id = invoice.get("debtor_id", "")
        audit.record(
            "statute.section_43b_h_refused",
            merchant_id=merchant_id,
            debtor_id=debtor_id,
            invoice_id=invoice.get("invoice_id", ""),
            refusal_reason=evaluation.refusal_reason,
        )
        return False, None, evaluation.refusal_reason

    return True, evaluation.compliant_notice_copy, None
