"""Seeded synthetic receivables ledger.

This is the load-bearing file of the build. Two properties matter more than anything
else here:

1. **Hidden behaviour parameters.** Every debtor carries a payment propensity, per
   channel responsiveness, promise reliability, dispute probability and a habitual
   number of days late. These are fixed by the seed before any agent runs and are never
   shown to the agent. `Debtor.agent_view()` is the only projection the strategist is
   allowed to see. Without that separation the reported recovery number is something the
   author chose rather than something the agent earned.

2. **Failure states are seeded, not bolted on.** TDS_UNDERPAID and PAID_OFF_RAIL exist in
   the ledger from the first run, so the decision engine has to handle them by
   construction rather than acquiring the ability later.

Determinism: everything draws from a single `random.Random(seed)` and iterates over
lists, never sets or dict key order. The same seed produces a byte-identical ledger, which
is what makes an agent-versus-baseline comparison meaningful.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

from app.config import PROJECT_ROOT

# The ledger is generated relative to a fixed "today" so that runs are reproducible
# across calendar days. Real deployment reads the clock; the experiment must not.
AS_OF = date(2026, 8, 26)

DATA_DIR = PROJECT_ROOT / "data"
LEDGER_PATH = DATA_DIR / "ledger.json"


class InvoiceState(StrEnum):
    OVERDUE = "OVERDUE"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    TDS_UNDERPAID = "TDS_UNDERPAID"
    PAID_OFF_RAIL = "PAID_OFF_RAIL"
    DISPUTED = "DISPUTED"
    PAID = "PAID"


class UdyamActivity(StrEnum):
    MANUFACTURING = "manufacturing"
    SERVICES = "services"
    TRADING = "trading"


@dataclass(frozen=True)
class Merchant:
    """The SME using this product. Statutory eligibility is a property of the supplier."""

    merchant_id: str
    name: str
    udyam_registered: bool
    udyam_category: str | None  # micro | small | medium | None
    udyam_activity: str | None


def msmed_eligible(merchant: Merchant) -> tuple[bool, str]:
    """Can this supplier invoke MSMED s.15/16 and the s.43B(h) argument?

    Verified 2026-08-26. Three independent gates, and failing any one of them means the
    statutory levers are unavailable:
      - Udyam registration must exist.
      - Only micro and small qualify. Medium is excluded.
      - A supplier whose Udyam activity is trading is excluded from the delayed payment
        provisions even though traders may register. Their registration confers priority
        sector lending benefits only.

    Returning the reason matters as much as the verdict: an agent that declines to cite a
    statute and explains why is the point, not an edge case.
    """
    if not merchant.udyam_registered:
        return False, "supplier is not registered on Udyam, so MSMED s.15/16 does not apply"
    if merchant.udyam_category == "medium":
        return False, "MSMED delayed payment provisions cover micro and small only, not medium"
    if merchant.udyam_activity == UdyamActivity.TRADING:
        return (
            False,
            "supplier is registered as a trader; traders are excluded from the MSMED "
            "delayed payment benefit and get priority sector lending benefits only",
        )
    return True, f"supplier is a registered {merchant.udyam_category} enterprise in {merchant.udyam_activity}"


@dataclass
class BehaviourParams:
    """Hidden ground truth. Never exposed to the agent, only to the simulator."""

    pay_propensity: float
    email_responsiveness: float
    whatsapp_responsiveness: float
    promise_reliability: float
    dispute_probability: float
    partial_payment_tendency: float
    habitual_days_late: int


AGENT_VISIBLE_FIELDS = (
    "debtor_id",
    "name",
    "relationship_since",
    "trailing_12m_value_paise",
    "preferred_channel",
    "language",
    "opted_out",
    "promises_made",
    "promises_kept",
    "prior_disputes",
    "avg_days_late",
)


def agent_view(debtor: dict) -> dict:
    """Project a debtor down to what the agent may see. Excludes `behaviour` by design.

    A whitelist, not a blacklist, and the single source of truth for the projection. The
    ledger is serialised with `asdict`, so a debtor loaded back from `data/ledger.json` is
    a plain dict still carrying the hidden parameters — every path that hands a debtor to
    the strategist or the envelope must come through here, or the reported recovery number
    is something the author chose rather than something the agent earned.

    `opted_out` is required rather than merely copied when present. Dropping it silently
    would let the envelope's suppression check see nothing at all, and a projection is not
    allowed to be the reason someone who opted out gets chased again.
    """
    if "opted_out" not in debtor:
        raise KeyError(
            f"debtor {debtor.get('debtor_id', '<unknown>')} has no `opted_out` field; "
            "suppression cannot be evaluated and the row must not reach the agent"
        )
    return {field: debtor[field] for field in AGENT_VISIBLE_FIELDS if field in debtor}


@dataclass
class Debtor:
    debtor_id: str
    name: str
    merchant_id: str
    relationship_since: int
    trailing_12m_value_paise: int
    preferred_channel: str
    language: str
    opted_out: bool
    promises_made: int
    promises_kept: int
    prior_disputes: int
    avg_days_late: float
    behaviour: BehaviourParams

    def agent_view(self) -> dict:
        """The only projection the strategist may see. Excludes `behaviour` by design."""
        return agent_view(vars(self))


@dataclass
class Invoice:
    invoice_id: str
    debtor_id: str
    merchant_id: str
    amount_paise: int
    invoice_date: str
    delivery_date: str
    written_agreement: bool
    objection_raised_within_15d: bool
    acceptance_date: str
    contractual_due_date: str
    statutory_due_date: str
    appointed_day: str
    days_overdue: int
    state: str
    amount_received_paise: int = 0
    tds_deducted_paise: int = 0
    off_rail_reference: str | None = None
    dispute_reason: str | None = None

    @property
    def outstanding_paise(self) -> int:
        """Naive outstanding. Does not subtract TDS — the strategist must handle that."""
        return self.amount_paise - self.amount_received_paise


DEBTOR_NAMES = [
    "Acme Industries Pvt Ltd", "Vertex Distributors", "Sundaram Auto Components",
    "Kaveri Textiles Pvt Ltd", "Meridian Logistics", "Prakash Engineering Works",
    "Nova Retail Systems", "Girija Chemicals Pvt Ltd", "Tandon Steel Traders",
    "Bluepeak Softworks", "Rajshree Packaging", "Everest Cold Chain",
    "Anand Electricals Pvt Ltd", "Coastal Marine Supplies", "Hexa Print Solutions",
    "Deccan Agro Processors", "Silverline Interiors", "Mahalaxmi Hardware",
    "Orbit Instruments Pvt Ltd", "Pushpa Foods Pvt Ltd",
]

DISPUTE_REASONS = [
    "Short delivery, 40 units received against 50 invoiced",
    "Rate on the invoice does not match the purchase order",
    "This invoice appears to duplicate INV-3902",
    "GST rate applied is 18 percent, our contract says 12 percent",
    "We already settled this by NEFT on 12 August, UTR 511923447",
]


def _weighted_amount(rng: random.Random) -> int:
    """Invoice values skewed toward the ICP: large enough that human chasing is expensive."""
    band = rng.random()
    if band < 0.15:
        rupees = rng.randint(40_000, 200_000)
    elif band < 0.70:
        rupees = rng.randint(200_000, 800_000)
    elif band < 0.93:
        rupees = rng.randint(800_000, 2_500_000)
    else:
        rupees = rng.randint(2_500_000, 6_000_000)
    return round(rupees, -2) * 100


def _build_merchants() -> list[Merchant]:
    # Two suppliers on purpose. The first can invoke the statute, the second cannot, and
    # the agent declining for the second is a named beat in the pitch.
    return [
        Merchant(
            merchant_id="MER-001",
            name="Nandi Precision Components",
            udyam_registered=True,
            udyam_category="small",
            udyam_activity=UdyamActivity.MANUFACTURING,
        ),
        Merchant(
            merchant_id="MER-002",
            name="Sagar Trading Company",
            udyam_registered=True,
            udyam_category="micro",
            udyam_activity=UdyamActivity.TRADING,
        ),
    ]


def _build_debtors(rng: random.Random, merchants: list[Merchant]) -> list[Debtor]:
    debtors: list[Debtor] = []
    for index, name in enumerate(DEBTOR_NAMES):
        # The trader merchant gets a small portfolio; it exists for the refusal beat.
        merchant = merchants[1] if index >= len(DEBTOR_NAMES) - 3 else merchants[0]

        promises_made = rng.randint(0, 12)
        promises_kept = rng.randint(max(0, promises_made - 4), promises_made)
        habitual = rng.choice([0, 2, 3, 5, 6, 8, 12, 15, 21, 30, 43])

        debtors.append(
            Debtor(
                debtor_id=f"DEB-{index + 1:03d}",
                name=name,
                merchant_id=merchant.merchant_id,
                relationship_since=rng.randint(2018, 2025),
                trailing_12m_value_paise=rng.randint(8, 900) * 100_000 * 100,
                preferred_channel=rng.choice(["email", "whatsapp", "email", "whatsapp", "email"]),
                # A minority of debtors read Hinglish more readily than formal English.
                language=rng.choice(["en", "en", "en", "hinglish", "hinglish"]),
                # One debtor has opted out. The envelope must honour it absolutely.
                opted_out=(index == 7),
                promises_made=promises_made,
                promises_kept=promises_kept,
                prior_disputes=rng.choice([0, 0, 0, 1, 1, 2]),
                avg_days_late=round(habitual + rng.uniform(-2, 3), 1),
                behaviour=BehaviourParams(
                    pay_propensity=round(rng.uniform(0.10, 0.75), 3),
                    email_responsiveness=round(rng.uniform(0.05, 0.70), 3),
                    whatsapp_responsiveness=round(rng.uniform(0.15, 0.85), 3),
                    promise_reliability=round(
                        (promises_kept / promises_made) if promises_made else rng.uniform(0.3, 0.8), 3
                    ),
                    dispute_probability=round(rng.uniform(0.01, 0.18), 3),
                    partial_payment_tendency=round(rng.uniform(0.0, 0.45), 3),
                    habitual_days_late=habitual,
                ),
            )
        )
    return debtors


def _statutory_dates(
    delivery: date, written_agreement: bool, objection: bool, rng: random.Random
) -> tuple[date, date, date]:
    """Acceptance, statutory due date, and appointed day under MSMED s.15.

    Verified 2026-08-26: the clock runs from acceptance or deemed acceptance, not from
    the contractual due date. Silence for 15 days after delivery is deemed acceptance. A
    written objection inside those 15 days moves acceptance to the date the objection is
    resolved, which is why a dispute does not merely pause chasing, it moves the legal
    due date.
    """
    if objection:
        acceptance = delivery + timedelta(days=rng.randint(16, 40))
    else:
        acceptance = delivery
    window = 45 if written_agreement else 15
    statutory_due = acceptance + timedelta(days=window)
    return acceptance, statutory_due, statutory_due + timedelta(days=1)


def _build_invoices(rng: random.Random, debtors: list[Debtor]) -> list[Invoice]:
    invoices: list[Invoice] = []
    counter = 4000

    # Deliberate, seeded distribution of the failure states. Roughly a fifth of the book
    # is something other than a plain overdue invoice, which is what makes the ladder
    # insufficient on its own.
    planned_states: list[str] = (
        [InvoiceState.TDS_UNDERPAID] * 6
        + [InvoiceState.PAID_OFF_RAIL] * 5
        + [InvoiceState.PARTIALLY_PAID] * 7
        + [InvoiceState.DISPUTED] * 4
        + [InvoiceState.OVERDUE] * 48
    )
    rng.shuffle(planned_states)

    for state in planned_states:
        debtor = rng.choice(debtors)
        counter += rng.randint(1, 9)

        amount = _weighted_amount(rng)
        days_overdue = rng.randint(1, 95)
        written_agreement = rng.random() < 0.7

        contractual_due = AS_OF - timedelta(days=days_overdue)
        delivery = contractual_due - timedelta(days=rng.randint(3, 30))
        invoice_date = delivery - timedelta(days=rng.randint(0, 4))
        objection = state == InvoiceState.DISPUTED and rng.random() < 0.5
        acceptance, statutory_due, appointed = _statutory_dates(
            delivery, written_agreement, objection, rng
        )

        invoice = Invoice(
            invoice_id=f"INV-{counter}",
            debtor_id=debtor.debtor_id,
            merchant_id=debtor.merchant_id,
            amount_paise=amount,
            invoice_date=invoice_date.isoformat(),
            delivery_date=delivery.isoformat(),
            written_agreement=written_agreement,
            objection_raised_within_15d=objection,
            acceptance_date=acceptance.isoformat(),
            contractual_due_date=contractual_due.isoformat(),
            statutory_due_date=statutory_due.isoformat(),
            appointed_day=appointed.isoformat(),
            days_overdue=days_overdue,
            state=str(state),
        )

        if state == InvoiceState.TDS_UNDERPAID:
            # Buyer withheld TDS and remitted the balance. They paid correctly and in
            # full; only a naive agent reads this as a shortfall.
            tds_rate = rng.choice([0.01, 0.02, 0.10])
            invoice.tds_deducted_paise = int(amount * tds_rate)
            invoice.amount_received_paise = amount - invoice.tds_deducted_paise
        elif state == InvoiceState.PAID_OFF_RAIL:
            # Settled by NEFT. Razorpay never sees it, so there is no suppression signal.
            invoice.amount_received_paise = amount
            invoice.off_rail_reference = f"UTR{rng.randint(100000000, 999999999)}"
        elif state == InvoiceState.PARTIALLY_PAID:
            invoice.amount_received_paise = int(amount * rng.uniform(0.2, 0.75) / 100) * 100
        elif state == InvoiceState.DISPUTED:
            invoice.dispute_reason = rng.choice(DISPUTE_REASONS)

        invoices.append(invoice)

    return invoices


def generate(seed: int) -> dict:
    """Build the whole ledger from one seed. Same seed, identical output."""
    rng = random.Random(seed)
    merchants = _build_merchants()
    debtors = _build_debtors(rng, merchants)
    invoices = _build_invoices(rng, debtors)

    return {
        "seed": seed,
        "as_of": AS_OF.isoformat(),
        "merchants": [asdict(m) for m in merchants],
        "debtors": [asdict(d) for d in debtors],
        "invoices": [asdict(i) for i in invoices],
    }


def fingerprint(ledger: dict) -> str:
    """Stable hash of the ledger, so reproducibility is checkable rather than asserted."""
    blob = json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def save(ledger: dict, path: Path = LEDGER_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    return path


def summarise(ledger: dict) -> str:
    invoices = ledger["invoices"]
    debtors = ledger["debtors"]
    merchants = {m["merchant_id"]: m for m in ledger["merchants"]}

    by_state: dict[str, int] = {}
    for inv in invoices:
        by_state[inv["state"]] = by_state.get(inv["state"], 0) + 1

    total = sum(i["amount_paise"] - i["amount_received_paise"] for i in invoices)
    multi = sum(
        1
        for d in debtors
        if sum(1 for i in invoices if i["debtor_id"] == d["debtor_id"]) > 1
    )

    lines = [
        f"seed {ledger['seed']}  fingerprint {fingerprint(ledger)}  as of {ledger['as_of']}",
        f"{len(merchants)} merchants, {len(debtors)} debtors, {len(invoices)} invoices",
        f"outstanding: Rs {total // 100:,}",
        f"debtors with more than one open invoice: {multi}  (grouping matters)",
        "",
        "invoice states:",
    ]
    for state, count in sorted(by_state.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {state:16} {count:3}")

    lines += ["", "statutory eligibility by merchant:"]
    for m in ledger["merchants"]:
        merchant = Merchant(**m)
        ok, reason = msmed_eligible(merchant)
        held = sum(1 for i in invoices if i["merchant_id"] == merchant.merchant_id)
        verdict = "ELIGIBLE" if ok else "NOT ELIGIBLE"
        lines.append(f"  {merchant.name:32} {verdict:13} ({held} invoices)")
        lines.append(f"      {reason}")

    opted_out = [d["name"] for d in debtors if d["opted_out"]]
    hinglish = sum(1 for d in debtors if d["language"] == "hinglish")
    lines += [
        "",
        f"opted out (escalation forbidden absolutely): {', '.join(opted_out) or 'none'}",
        f"debtors preferring Hinglish: {hinglish}",
    ]
    return "\n".join(lines)


def _self_check() -> None:
    """The reproducibility guarantee is the whole point, so it is checked, not claimed."""
    a, b = generate(42), generate(42)
    assert fingerprint(a) == fingerprint(b), "same seed produced different ledgers"
    assert fingerprint(generate(43)) != fingerprint(a), "different seeds collided"

    invoices = a["invoices"]
    states = {i["state"] for i in invoices}
    for required in ("TDS_UNDERPAID", "PAID_OFF_RAIL", "PARTIALLY_PAID", "DISPUTED"):
        assert required in states, f"{required} missing from the seeded ledger"

    # A TDS invoice must look short paid but be fully settled by the buyer.
    tds = [i for i in invoices if i["state"] == "TDS_UNDERPAID"]
    assert tds and all(
        i["amount_received_paise"] + i["tds_deducted_paise"] == i["amount_paise"] for i in tds
    ), "TDS invoices must reconcile to face value"

    # Off rail invoices are fully settled and carry a reference the agent can ask for.
    off = [i for i in invoices if i["state"] == "PAID_OFF_RAIL"]
    assert off and all(
        i["amount_received_paise"] == i["amount_paise"] and i["off_rail_reference"] for i in off
    ), "off rail invoices must be fully paid with a UTR"

    # The statutory window must follow acceptance, not the contractual due date.
    for inv in invoices:
        acceptance = date.fromisoformat(inv["acceptance_date"])
        statutory = date.fromisoformat(inv["statutory_due_date"])
        expected = 45 if inv["written_agreement"] else 15
        assert (statutory - acceptance).days == expected, "statutory window miscomputed"

    # Exactly one merchant may invoke the statute; the trader may not.
    verdicts = [msmed_eligible(Merchant(**m))[0] for m in a["merchants"]]
    assert verdicts == [True, False], "the refusal case is missing from the ledger"

    # The agent must never be handed the hidden parameters.
    debtor = Debtor(**{**a["debtors"][0], "behaviour": BehaviourParams(**a["debtors"][0]["behaviour"])})
    assert "behaviour" not in debtor.agent_view(), "hidden parameters leaked into the agent view"

    print("ok  ledger self check passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the seeded receivables ledger.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--check", action="store_true", help="run the self check and exit")
    parser.add_argument("--write", action="store_true", help="write data/ledger.json")
    args = parser.parse_args()

    if args.check:
        _self_check()
    else:
        ledger = generate(args.seed)
        print(summarise(ledger))
        if args.write:
            print(f"\nwrote {save(ledger).relative_to(PROJECT_ROOT)}")
