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

import re
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


def _roots(*fragments: str) -> re.Pattern[str]:
    """Compile an alternation of keyword-root fragments into one case-insensitive pattern.

    Each fragment is an unanchored regex, so a root like ``short[-\\s]?deliver`` covers
    "short delivery", "short-delivered" and "short deliver" without listing each. Anchor a
    root with ``\\b`` wherever it is also a substring of an unrelated word — bare ``rate``
    hides inside "corporate"/"separate", bare ``spec`` inside "unspecified" — otherwise the
    root silently misclassifies unrelated text.
    """
    return re.compile("|".join(fragments), re.IGNORECASE)


# The first pattern that matches wins, so this sequence *is* the classifier's precedence,
# not just a list. It is ordered by which claim is the narrower question, NOT by how
# specific the patterns happen to be: a claim that the debt is already settled, or that
# the invoice went to the wrong legal entity, outranks any claim about what was delivered;
# a pricing dispute (INVOICE_MISMATCH) outranks a quantity/condition dispute
# (GOODS_SERVICES) because the two overlap ("40 billed, 32 received") and the money figure
# is the narrower question.
#
# Because a broad root high in this table beats a precise root low in it, every pattern
# above GOODS_SERVICES has to match a *claim* rather than a passing mention - a reason
# that merely says the word "tax" is not a tax dispute. Reordering these rows, or
# loosening a root in one, changes classifications that the resolution page (evidence
# checklist) and the audit log both persist.
_CATEGORY_PATTERNS: tuple[tuple[DisputeCategory, re.Pattern[str]], ...] = (
    (
        DisputeCategory.ALREADY_PAID,
        _roots(
            r"\bneft", r"\brtgs", r"\bimps", r"\butr\b", r"\bupi\b", r"\bnach\b",
            r"already (paid|settled|cleared|remitted|reconciled|made)",
            r"paid (in full|via|by|vide|through)", r"\bpaid on\b",
            r"payment (made|done|sent|released|processed|remitted|effected)",
            r"\bremitt", r"cheque (no|number|dated|issued|sent|cleared)", r"demand draft",
            r"\bdd no\b", r"transaction (id|ref|reference|number)", r"\btxn\b",
            r"bank (transfer|statement)", r"wire transfer", r"funds (transferred|sent|released)",
            r"no (dues|outstanding|amount due)",
            r"nothing (is )?(outstanding|due|pending|payable|owed)",
            r"account (is )?(settled|reconciled)",
        ),
    ),
    (
        DisputeCategory.DUPLICATE,
        _roots(
            r"\bduplicat",
            r"\btwice\b.{0,20}(bill|invoic|charg)", r"(bill|invoic|charg|rais)\w*.{0,20}\btwice\b",
            r"double (bill|invoic|charg|payment|paid)",
            r"same invoice (again|twice|number)", r"same (bill|amount) (again|twice)",
            r"already (invoiced|billed)",
            r"(second|another) (invoice|bill) for (the same|this)",
            r"two (invoices|bills) for", r"repeat(ed)? (invoice|bill|billing)",
        ),
    ),
    (
        DisputeCategory.TAX_GST,
        _roots(
            r"\bgst\b", r"\bgstr", r"\bhsn\b", r"\bsac code", r"\btds\b", r"\btcs\b",
            r"\bcess\b", r"\bi[- ]?gst", r"\bc[- ]?gst", r"\bs[- ]?gst", r"\butgst",
            # Claim-shaped, not a bare root. `\btax` matched any passing mention of tax
            # and, from this row, beat an explicit short-delivery claim further down:
            # "short delivered, and the tax on the invoice is also wrong" classified as
            # TAX_GST and asked the debtor for a GSTR-2B certificate. Every real tax
            # dispute in this domain names an artefact, and those are the roots above.
            r"\btax (rate|amount|percent|percentage|mismatch|deduct|credit|component|"
            r"calculation|invoice|slab|head)",
            r"(wrong|incorrect|excess|short|extra|higher|lower) tax\b",
            r"input (tax )?credit", r"\bitc\b", r"\b2b\b", r"\b26as\b",
            r"reverse charge", r"\brcm\b", r"withholding",
        ),
    ),
    (
        DisputeCategory.WRONG_RECIPIENT,
        _roots(
            r"wrong (entity|compan|firm|subsidiar|branch|division|address|party|"
            r"business|unit\b|legal entity|department|gstin|recipient)",
            r"subsidiary entity",
            r"not our (compan|invoice|bill|order|purchase|dues|liability|account)",
            r"not (addressed|meant|intended|billed) (to|for) us",
            r"different (entity|compan|legal entity|firm|gstin)",
            r"sister (compan|concern)", r"group (compan|entity)",
            r"another (entity|compan|branch|unit|division)",
            r"(wrong|incorrect) gstin", r"gstin (is )?(wrong|incorrect|mismatch|does not match)",
            r"billed to the wrong", r"raised (on|against) the wrong",
            r"addressed to (another|a different|the wrong)",
            r"belongs to (another|a different)", r"meant for (another|a different)",
            r"we are not the (right|correct|intended)",
        ),
    ),
    (
        DisputeCategory.INVOICE_MISMATCH,
        _roots(
            r"\brate\b", r"\brates\b", r"\bprice", r"\bpricing", r"unit (rate|price|cost)",
            r"purchase order", r"\bpo (rate|price|value|amount|number|copy|terms)", r"\bp\.o\.",
            r"overcharg", r"over[- ]?charg", r"over[- ]?bill", r"overbill",
            r"excess (amount|billing|charge|charged)",
            r"(wrong|incorrect|inflated|excess) amount",
            r"amount (is wrong|is incorrect|does not match|doesn'?t match|mismatch|billed wrong)",
            r"\bmismatch", r"discrepan", r"does not (tally|add up|match)",
            r"doesn'?t (tally|add up|match)",
            r"(calculation|computation|arithmetic|totall?ing) (error|mistake|is wrong)",
            r"wrongly (calculated|billed|charged)", r"miscalculat", r"line[- ]?item",
            r"\bquoted", r"as per (the )?quot", r"agreed (rate|price|amount|cost)",
            r"(higher|more|lower|less) than (the )?(agreed|quoted|po|our po|contracted)",
            r"extra (charge|amount|cost)", r"additional (charge|cost)", r"hidden (charge|cost)",
            r"freight (charged|added|billed)", r"discount (not|missing|omitted|error|has not)",
        ),
    ),
    (
        DisputeCategory.CONTRACTUAL,
        _roots(
            r"\bcontract\b", r"contractual", r"\bsla\b", r"\bmilestone", r"\bclause",
            r"terms of (agreement|the contract|payment)", r"\bagreement\b", r"\bmou\b", r"\bloi\b",
            r"payment terms", r"credit (period|terms|days)", r"net[- ]?(15|30|45|60|90)\b",
            r"not (yet )?due", r"not due yet", r"not payable (yet|until)", r"prematur",
            r"too early to", r"before (the )?due date", r"retention", r"hold[- ]?back", r"\bholdback",
            r"warranty period", r"defect liability", r"completion certificate",
            r"(work|project|job|service delivery|scope) (is )?(not|incomplete|pending|unfinished)",
            r"not (signed off|signed-off|accepted|approved|completed|commissioned)",
            r"sign[- ]?off (pending|awaited|not done)",
            r"pending (sign[- ]?off|approval|acceptance|inspection)",
            r"out of scope", r"scope of work", r"advance (not )?adjust", r"security deposit",
            r"stage payment", r"as per (the )?(contract|agreement|terms|mou|loi|understanding)",
        ),
    ),
    (
        DisputeCategory.GOODS_SERVICES,
        _roots(
            r"\bshort[-\s]?(deliver|ship|shipp|suppl|receiv|land|dispatch|qty|quantit|fall|age)",
            r"\bshortfall", r"\bshortage", r"under[- ]?(deliver|suppl|shipp)",
            r"less (quantity|units|material|stock)", r"fewer (unit|item|piece|quantit)",
            r"missing (unit|item|piece|quantit|goods|stock|material|part|box|carton)",
            r"units (short|missing|less|received|not received)",
            r"not (yet )?(received|delivered)",
            r"never (received|delivered|arrived|got|reached|came)",
            r"nothing (was )?(received|delivered|arrived)",
            r"(goods|material|stock|consignment|shipment|item|order|parcel) not "
            r"(received|delivered|reached)",
            r"non[- ]?delivery", r"undelivered", r"haven'?t received", r"have not received",
            r"yet to receive", r"partial (delivery|shipment|dispatch|supply)",
            r"incomplete (delivery|order|shipment|supply|consignment)",
            r"wrong (item|product|material|goods|size|spec|model|grade|colou?r|quantit|"
            r"quality|batch|make|variant)",
            r"not as per (spec|sample|drawing|order|description|approved)",
            r"sub[- ]?standard", r"(poor|bad|inferior|low) quality",
            r"quality (issue|problem|concern|fail|not|is)", r"\bquality\b",
            r"\bdefect", r"\bfaulty",
            r"\bbroken", r"\bdamage", r"spoil", r"\bexpired", r"\brejected", r"\brejection",
            r"failed (qc|inspection|testing|quality)", r"qc fail",
            r"not (usable|working|functioning)", r"does ?n'?t work", r"stopped working",
            r"\bgoods\b", r"\bservice", r"not (rendered|performed|carried out)",
            r"(work|job|service) (not|never) (done|performed|completed|carried out)",
            r"install(ation|ed)? (pending|not done|incomplete)",
        ),
    ),
)


def classify_dispute_reason(reason_text: str | None) -> DisputeCategory:
    """Classify a free-form debtor dispute description into the standard taxonomy.

    Matches on word roots, not exact phrases, so natural phrasings ("short delivered",
    "billed twice", "paid via NEFT") land in the right category. Precedence is defined by
    the order of ``_CATEGORY_PATTERNS``.

    Widening a keyword list can narrow it: moving to roots silently dropped "paid on",
    "wrong recipient", "subsidiary entity", "units received" and a bare "quality", all of
    which the exact-phrase lists had matched, and a debtor asserting they had already paid
    was left holding the UNKNOWN evidence checklist. Those five are pinned by
    ``test_dispute_classification_no_phrasing_lost_to_roots``; anything taken out of this
    table needs the same treatment.
    """
    if not reason_text or not reason_text.strip():
        return DisputeCategory.UNKNOWN

    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(reason_text):
            return category

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
    else:
        # Objection was raised after 15 days: deemed acceptance on delivery date stands
        acceptance = delivery_date

    statutory_due = acceptance + timedelta(days=window)
    # The appointed day is the day after the statutory due date in both branches — there is
    # no separate "15 days from acceptance" formula to restate: window is 15 in the
    # no-written-agreement case, so acceptance + 16 and statutory_due + 1 are the same date.
    # One expression means a change to the window constant cannot desync it from this line.
    appointed = statutory_due + timedelta(days=1)
    return acceptance, statutory_due, appointed
