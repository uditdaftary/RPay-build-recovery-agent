"""End-to-End Recovery Pipeline Orchestration.

Connects:
1. Hard Policy Envelope & AI Strategist (Stage 1 & 2)
2. Outbound Message Drafting & Anti-Dark-Pattern Filter (Stage 3)
3. Multi-Channel Dispatchers & Review-First Operator Queue (Stage 4)
4. Master Kill Switch & Audit Trail Logging
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app import audit
from app.channels import dispatch_message
from app.envelope import ActionClass, Channel
from app.messages import draft_message_for_decision
from app.operator import is_kill_switch_active, queue_for_review

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineRunResult:
    total_evaluated: int
    automated_dispatches: int
    review_queued: int
    suppressed_opt_out: int
    kill_switch_blocked: int


def execute_recovery_pipeline(
    ledger: dict[str, Any],
    as_of: date,
    *,
    dry_run: bool = False,
) -> PipelineRunResult:
    """Execute complete end-to-end recovery pipeline across all ledger debtors."""
    from run_experiment import run_deterministic_agent_batch

    # Ensure strategist batch evaluation respects caller's as_of date
    ledger["as_of"] = as_of.isoformat()

    # Use prompt-grounded deterministic evaluator for offline reproducibility
    decisions = run_deterministic_agent_batch(ledger)
    debtors_by_id = {d["debtor_id"]: d for d in ledger["debtors"]}
    merchants_by_id = {m["merchant_id"]: m for m in ledger["merchants"]}
    invoices_by_debtor: dict[str, list[dict]] = {}
    for inv in ledger["invoices"]:
        invoices_by_debtor.setdefault(inv["debtor_id"], []).append(inv)

    automated_dispatches = 0
    review_queued = 0
    suppressed_opt_out = 0
    kill_switch_blocked = 0

    for dec in decisions:
        debtor = debtors_by_id.get(dec.debtor_id, {})
        d_invoices = invoices_by_debtor.get(dec.debtor_id, [])

        debtor_id_str = debtor.get("debtor_id") or dec.debtor_id
        debtor_name_str = debtor.get("name") or dec.debtor_name
        name_clean = re.sub(r"[^a-z0-9]", "", debtor_name_str.lower()) if debtor_name_str else ""
        fallback_email = f"{debtor_id_str.lower()}@{name_clean}.in" if name_clean else f"{debtor_id_str.lower()}@example.com"
        recipient_email = debtor.get("email") or fallback_email
        recipient_phone = debtor.get("phone") or "+919876543210"

        merchant_id = debtor.get("merchant_id")
        merchant = merchants_by_id.get(merchant_id) if merchant_id else None

        if debtor.get("opted_out"):
            suppressed_opt_out += 1

        if is_kill_switch_active():
            kill_switch_blocked += 1
            audit.record(
                "pipeline.kill_switch_blocked",
                debtor_id=dec.debtor_id,
                strategy=dec.strategy.value,
            )
            continue

        if not merchant:
            audit.record(
                "pipeline.merchant_unresolved",
                debtor_id=dec.debtor_id,
                merchant_id=merchant_id,
            )
            draft = draft_message_for_decision(
                dec,
                debtor,
                d_invoices,
                {},
                as_of=as_of,
                recipient_email=recipient_email,
                recipient_phone=recipient_phone,
            )
            queue_for_review(
                debtor_id=dec.debtor_id,
                debtor_name=dec.debtor_name,
                strategy=dec.strategy.value,
                ask_amount_paise=dec.ask_amount_paise or 0,
                reasoning=f"Unresolved merchant ({merchant_id}): {dec.reasoning}",
                draft=draft,
                recipient_email=recipient_email,
                recipient_phone=recipient_phone,
            )
            review_queued += 1
            continue

        draft = draft_message_for_decision(
            dec,
            debtor,
            d_invoices,
            merchant,
            as_of=as_of,
            recipient_email=recipient_email,
            recipient_phone=recipient_phone,
        )

        if dec.review_required or dec.action_class == ActionClass.REVIEW_REQUIRED:
            queue_for_review(
                debtor_id=dec.debtor_id,
                debtor_name=dec.debtor_name,
                strategy=dec.strategy.value,
                ask_amount_paise=dec.ask_amount_paise or 0,
                reasoning=dec.reasoning,
                draft=draft,
                recipient_email=recipient_email,
                recipient_phone=recipient_phone,
            )
            review_queued += 1
        elif dec.channel != Channel.NONE:
            dispatch_message(
                draft,
                recipient_email=recipient_email,
                recipient_phone=recipient_phone,
                dry_run=dry_run,
            )
            automated_dispatches += 1

    result = PipelineRunResult(
        total_evaluated=len(decisions),
        automated_dispatches=automated_dispatches,
        review_queued=review_queued,
        suppressed_opt_out=suppressed_opt_out,
        kill_switch_blocked=kill_switch_blocked,
    )

    audit.record(
        "pipeline.completed",
        total_evaluated=result.total_evaluated,
        automated_dispatches=result.automated_dispatches,
        review_queued=result.review_queued,
        suppressed_opt_out=result.suppressed_opt_out,
        kill_switch_blocked=result.kill_switch_blocked,
    )

    return result
