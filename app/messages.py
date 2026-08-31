"""Outbound message drafting engine and anti-dark-pattern guardrails.

Stage 3 of the recovery pipeline: Converts StrategistDecision into calibrated,
statute-compliant, anti-dark-pattern verified communications across Email and WhatsApp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app import audit
from app.envelope import Channel, Language, Strategy, Tone
from app.ledger import invoice_collectible_paise
from app.statute import (
    DEFAULT_RBI_BANK_RATE_PCT,
    STATUTORY_RATE_MULTIPLIER,
    calculate_section_16_interest,
    compute_statutory_dates,
    evaluate_section_43b_h,
    validate_section_43b_h_copy,
)
from app.strategist import StrategistDecision


@dataclass(frozen=True)
class DraftedMessage:
    debtor_id: str
    channel: Channel
    language: Language
    tone: Tone
    subject: str
    body: str
    is_statutory: bool
    dark_pattern_clean: bool
    recipient_email: str | None = None
    recipient_phone: str | None = None


def validate_and_sanitize_copy(text: str) -> tuple[bool, str | None]:
    """Check message copy against prohibited anti-dark-pattern phrases."""
    return validate_section_43b_h_copy(text)


def _format_inr_rupees(paise: int) -> str:
    return f"Rs {paise // 100:,}"


def _statutory_due_date(invoice: dict[str, Any], as_of: date) -> date:
    """The MSMED s.15 due date for one invoice, run from acceptance rather than the contract.

    Falls back to the contractual date only when the invoice carries neither a delivery nor
    an invoice date, because without one there is nothing to start the statutory clock from.
    """
    raw_delivery = invoice.get("delivery_date") or invoice.get("invoice_date")
    if raw_delivery:
        _, statutory_due, _ = compute_statutory_dates(
            date.fromisoformat(raw_delivery),
            written_agreement=invoice.get("written_agreement", True),
        )
        if statutory_due is not None:
            return statutory_due
    raw_due = invoice.get("contractual_due_date")
    return date.fromisoformat(raw_due) if raw_due else as_of


def _accrued_section_16_interest(
    invoices: list[dict[str, Any]], as_of: date
) -> tuple[int, float]:
    """Section 16 interest summed per invoice, each priced from its own statutory due date.

    One invoice's clock must not price another's balance. Computing once on the debtor's
    aggregate charged a 20-day-overdue invoice at a 90-day-overdue invoice's rate, which
    overstates the figure in a formal demand notice. Only invoices with something still
    collectible accrue: a balance settled by TDS credit or off-rail payment owes nothing.

    Returns (accrued_interest_paise, annual_rate_pct).
    """
    accrued = 0
    rate_pct = float(DEFAULT_RBI_BANK_RATE_PCT * STATUTORY_RATE_MULTIPLIER)
    for invoice in invoices:
        collectible = invoice_collectible_paise(invoice)
        if collectible <= 0:
            continue
        result = calculate_section_16_interest(
            collectible, _statutory_due_date(invoice, as_of), as_of
        )
        accrued += result.accrued_interest_paise
        rate_pct = result.annual_rate_pct
    return accrued, rate_pct


def draft_message_for_decision(
    decision: StrategistDecision,
    debtor: dict[str, Any],
    invoices: list[dict[str, Any]],
    merchant: dict[str, Any],
    *,
    as_of: date,
    recipient_email: str | None = None,
    recipient_phone: str | None = None,
) -> DraftedMessage:
    """Draft concrete communication copy for a strategist recovery decision."""
    debtor_name = debtor.get("name", decision.debtor_name)
    merchant_name = merchant.get("name", "Supplier")
    lang_str = str(debtor.get("language") or decision.language).lower()
    is_hinglish = "hinglish" in lang_str or lang_str == "hi"
    actual_lang = Language.HINGLISH if is_hinglish else Language.EN

    recipient_email = recipient_email or debtor.get("recipient_email") or debtor.get("email")
    recipient_phone = recipient_phone or debtor.get("recipient_phone") or debtor.get("phone")

    resolution_url = decision.resolution_url or ""
    link_line = f"\nResolution Portal: {resolution_url}" if resolution_url else ""

    collectible_invoices = [i for i in invoices if invoice_collectible_paise(i) > 0]
    primary_invoice = (
        max(collectible_invoices, key=lambda inv: inv.get("days_overdue", 0), default=None)
        if collectible_invoices
        else (max(invoices, key=lambda inv: inv.get("days_overdue", 0), default=None) if invoices else {})
    )
    if primary_invoice is None:
        primary_invoice = {}

    invoice_ids = ", ".join(i.get("invoice_id", "") for i in invoices if "invoice_id" in i) if invoices else ""
    ask_paise = decision.ask_amount_paise if decision.ask_amount_paise is not None else sum(i.get("amount_paise", 0) for i in invoices)
    ask_display = _format_inr_rupees(ask_paise)

    subject = ""
    body = ""
    is_statutory = False

    if decision.strategy == Strategy.WAIT:
        subject = f"Internal Note: Recovery on hold for {debtor_name}"
        body = f"Recovery action held for {debtor_name} under policy restraint. Reasoning: {decision.reasoning}"
        return DraftedMessage(
            debtor_id=decision.debtor_id,
            channel=Channel.NONE,
            language=actual_lang,
            tone=Tone.NEUTRAL,
            subject=subject,
            body=body,
            is_statutory=False,
            dark_pattern_clean=True,
            recipient_email=recipient_email,
            recipient_phone=recipient_phone,
        )

    if decision.strategy == Strategy.HUMAN_HANDOFF:
        subject = f"Action Required: Human Credit Specialist Review - {debtor_name}"
        body = (
            f"Debtor {debtor_name} ({decision.debtor_id}) requires manual specialist review.\n"
            f"Reasoning: {decision.reasoning}\n"
            f"Open Invoices: {invoice_ids}\n"
            f"Collectible Balance: {ask_display}"
        )
        return DraftedMessage(
            debtor_id=decision.debtor_id,
            channel=Channel.NONE,
            language=actual_lang,
            tone=Tone.NEUTRAL,
            subject=subject,
            body=body,
            is_statutory=False,
            dark_pattern_clean=True,
            recipient_email=recipient_email,
            recipient_phone=recipient_phone,
        )

    if decision.strategy == Strategy.RECONCILE:
        has_tds = any(i.get("state") == "TDS_UNDERPAID" for i in invoices)

        if has_tds:
            if is_hinglish:
                subject = f"TDS Certificate Request - {merchant_name} ({invoice_ids})"
                body = (
                    f"Namaste {debtor_name},\n\n"
                    f"Aapke account par invoice(s) {invoice_ids} ke against TDS deduction note hua hai. "
                    f"Kripya Form 26AS certificate ya challan details share karein taaki hum ledger reconcile kar sakein.\n"
                    f"{link_line}\n\n"
                    f"Dhanyawad,\n{merchant_name}"
                )
            else:
                subject = f"TDS Reconciliation Request: Invoices {invoice_ids} - {merchant_name}"
                body = (
                    f"Dear Accounts Team at {debtor_name},\n\n"
                    f"We noted a TDS deduction on invoice(s) {invoice_ids}. To update our ledgers and issue credit, "
                    f"please share the corresponding Form 26AS TDS certificate or challan details.\n"
                    f"{link_line}\n\n"
                    f"Warm regards,\nFinance Team, {merchant_name}"
                )
        else:
            if is_hinglish:
                subject = f"Bank Transfer UTR Verification - {merchant_name} ({invoice_ids})"
                body = (
                    f"Namaste {debtor_name},\n\n"
                    f"Invoice(s) {invoice_ids} ke direct bank transfer settlement ko verify karne ke liye "
                    f"kripya bank NEFT/RTGS UTR reference number share karein.\n"
                    f"{link_line}\n\n"
                    f"Dhanyawad,\n{merchant_name}"
                )
            else:
                subject = f"Payment Verification (UTR Confirmation) - {merchant_name}"
                body = (
                    f"Dear Accounts Team at {debtor_name},\n\n"
                    f"To reconcile your direct bank remittance for invoice(s) {invoice_ids}, "
                    f"please confirm the NEFT/RTGS UTR transaction reference.\n"
                    f"{link_line}\n\n"
                    f"Warm regards,\nFinance Team, {merchant_name}"
                )

    elif decision.strategy == Strategy.RESOLVE_DISPUTE:
        if is_hinglish:
            subject = f"Dispute Resolution - Invoice {invoice_ids} - {merchant_name}"
            body = (
                f"Namaste {debtor_name},\n\n"
                f"Aapki invoice(s) {invoice_ids} ke regarding dispute request receive hui hai. "
                f"Payment recovery ko pause kar diya gaya hai. Hamari team objection review kar rahi hai. "
                f"Kripya related supporting documents portal par upload karein.\n"
                f"{link_line}\n\n"
                f"Dhanyawad,\n{merchant_name}"
            )
        else:
            subject = f"Dispute Triage & Clarification: Invoices {invoice_ids} - {merchant_name}"
            body = (
                f"Dear {debtor_name},\n\n"
                f"We acknowledge your objection regarding invoice(s) {invoice_ids}. Automated recovery has been "
                f"paused while our team reviews the issue. Please submit supporting documentation via the resolution link.\n"
                f"{link_line}\n\n"
                f"Sincerely,\n{merchant_name}"
            )

    elif decision.strategy == Strategy.ESCALATE:
        # Check statutory eligibility
        stat_eval = evaluate_section_43b_h(merchant, primary_invoice, as_of)
        days_overdue = primary_invoice.get("days_overdue") or 0
        if stat_eval.is_eligible and days_overdue > 15:
            is_statutory = True
            # Section 16 interest, summed per invoice from each invoice's own statutory due
            # date under s.15. The principal demanded stays the envelope-approved ask, which
            # may already carry a clamp; the interest is what the balances actually accrued.
            accrued_interest_paise, annual_rate_pct = _accrued_section_16_interest(invoices, as_of)
            interest_disp = _format_inr_rupees(accrued_interest_paise)
            total_payable_disp = _format_inr_rupees(ask_paise + accrued_interest_paise)

            if is_hinglish:
                subject = f"Formal Statutory Notice: Section 15/16 MSMED Act - Invoices {invoice_ids}"
                body = (
                    f"Dear {debtor_name},\n\n"
                    f"Invoice(s) {invoice_ids} overdue hain. As per MSMED Act 2006 Section 15/16, statutory compound "
                    f"interest of {interest_disp} (at 3x RBI rate) has accrued.\n\n"
                    f"Important Tax Notice: Section 43B(h) of the Income Tax Act ke tahat outstanding dues year-end par "
                    f"disallow ho sakte hain if unpaid.\n\n"
                    f"Kripya total payable amount {total_payable_disp} clear karein: {link_line}\n\n"
                    f"Authorized Signatory,\n{merchant_name}"
                )
            else:
                subject = f"Formal Statutory Notice: Overdue MSMED Invoices {invoice_ids} - {merchant_name}"
                body = (
                    f"To the Board of Directors / Finance Department, {debtor_name}:\n\n"
                    f"Re: Statutory demand for outstanding dues on Invoice(s) {invoice_ids} ({ask_display}).\n\n"
                    f"Under Section 15 and Section 16 of the Micro, Small and Medium Enterprises Development (MSMED) Act, 2006, "
                    f"mandatory compound interest with monthly rests at 3x the RBI Bank Rate ({annual_rate_pct:.2f}% p.a.) "
                    f"accrues on delayed sums, currently amounting to {interest_disp}.\n\n"
                    f"Statutory Tax Note: {stat_eval.compliant_notice_copy}\n\n"
                    f"Please settle the outstanding balance via the secure portal:{link_line}\n\n"
                    f"Sincerely,\nCredit & Legal Department, {merchant_name}"
                )
        else:
            # Refusal path: Supplier is ineligible trader or invoice not eligible
            is_statutory = False
            refusal_reason = stat_eval.refusal_reason or "Invoice overdue days <= 15 grace threshold"
            audit.record(
                "statute.section_43b_h_refused",
                merchant_id=merchant.get("merchant_id", ""),
                debtor_id=decision.debtor_id,
                refusal_reason=refusal_reason,
            )
            if is_hinglish:
                subject = f"Urgent Commercial Payment Escalation - {merchant_name} ({invoice_ids})"
                body = (
                    f"Dear {debtor_name},\n\n"
                    f"Invoice(s) {invoice_ids} ({ask_display}) overdue hain. Hamara commercial payment reminder "
                    f"hai ki kripya balance settle karein:\n"
                    f"{link_line}\n\n"
                    f"Regards,\n{merchant_name}"
                )
            else:
                subject = f"Commercial Payment Escalation: Invoices {invoice_ids} - {merchant_name}"
                body = (
                    f"Dear Accounts Team at {debtor_name},\n\n"
                    f"This is a formal commercial payment reminder regarding overdue invoice(s) {invoice_ids} "
                    f"with an outstanding balance of {ask_display}. Please arrange immediate settlement.\n"
                    f"{link_line}\n\n"
                    f"Sincerely,\nAccounts Department, {merchant_name}"
                )

    elif decision.strategy == Strategy.OBTAIN_PROMISE:
        if is_hinglish:
            subject = f"Payment Commitment Request - {merchant_name} ({invoice_ids})"
            body = (
                f"Namaste {debtor_name},\n\n"
                f"Invoice(s) {invoice_ids} balance {ask_display} pending hai. Kripya portal par confirm karein ki aap "
                f"kis date tak payment execute karenge.\n"
                f"{link_line}\n\n"
                f"Dhanyawad,\n{merchant_name}"
            )
        else:
            subject = f"Payment Commitment Request: Invoices {invoice_ids} - {merchant_name}"
            body = (
                f"Dear {debtor_name},\n\n"
                f"We are following up on invoice(s) {invoice_ids} with an outstanding balance of {ask_display}. "
                f"Please confirm your expected payment date via our resolution portal:\n"
                f"{link_line}\n\n"
                f"Warm regards,\n{merchant_name}"
            )

    else:
        # REQUEST_PAYMENT and NEGOTIATE_PARTIAL share this copy. They were split into two
        # branches that rendered byte-identical English, which is two templates to keep in
        # step for no difference in what the debtor reads. Split them again only when the
        # concession ask genuinely needs to read differently from the reminder.
        if is_hinglish:
            subject = f"Payment Reminder - {merchant_name} ({invoice_ids})"
            body = (
                f"Namaste {debtor_name},\n\n"
                f"Aapke invoice(s) {invoice_ids} ka payment amount {ask_display} due hai. "
                f"Kripya link par click karke payment clear karein:\n"
                f"{link_line}\n\n"
                f"Dhanyawad,\n{merchant_name}"
            )
        else:
            subject = f"Payment Reminder: Invoice(s) {invoice_ids} - {merchant_name}"
            body = (
                f"Dear {debtor_name},\n\n"
                f"This is a reminder that payment for invoice(s) {invoice_ids} in the amount of {ask_display} "
                f"is currently outstanding. You can review and settle this invoice online:\n"
                f"{link_line}\n\n"
                f"Warm regards,\n{merchant_name}"
            )

    # Anti-Dark-Pattern Verification & Sanitization
    is_clean, failure_reason = validate_and_sanitize_copy(body)
    initial_clean = is_clean
    if not is_clean:
        audit.record(
            "message.rejected_dark_pattern",
            debtor_id=decision.debtor_id,
            reason=failure_reason,
            original_body=body,
        )
        body = (
            f"Dear {debtor_name},\n\n"
            f"Please be reminded of outstanding invoice(s) {invoice_ids} totaling {ask_display}. "
            f"You may review the details and settle through the portal:{link_line}\n\n"
            f"Sincerely,\n{merchant_name}"
        )

    draft = DraftedMessage(
        debtor_id=decision.debtor_id,
        channel=decision.channel,
        language=actual_lang,
        tone=decision.tone,
        subject=subject,
        body=body,
        is_statutory=is_statutory,
        dark_pattern_clean=initial_clean,
        recipient_email=recipient_email,
        recipient_phone=recipient_phone,
    )

    audit.record(
        "message.drafted",
        debtor_id=decision.debtor_id,
        channel=draft.channel.value,
        language=draft.language.value,
        tone=draft.tone.value,
        is_statutory=draft.is_statutory,
        subject=draft.subject,
    )

    return draft
