"""Indian payment reconciliation. Pure code. It only ever proposes; posting is G-PAY's decision."""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations

from dlra.models import Allocation, Customer, Invoice, Payment, fmt_inr
from dlra.policy import Tds

MAX_OPEN_INVOICES = 12
MAX_SUBSET = 4
AMBIGUOUS_GAP = 0.15
REFERENCE_CONFIDENCE = 0.98
UNREFERENCED_CAP = 0.90  # an unreferenced match can never reach the auto-post threshold
_SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "co", "and"}


def normalise_name(name: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", name.casefold()).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def name_score(text: str, customer: Customer) -> float:
    target = normalise_name(text)
    return max(
        SequenceMatcher(None, target, normalise_name(n)).ratio()
        for n in (customer.canonical_name, *customer.aliases)
    )


def payer_score(payment: Payment, customer: Customer) -> float:
    if customer.gstin and customer.gstin.casefold() in payment.narration.casefold():
        return 1.0
    return name_score(payment.payer_name, customer) if payment.payer_name else 0.5


def resolve_payer(payment: Payment, customers: list[Customer], floor: float = 0.8) -> Customer | None:
    best = max(customers, key=lambda c: payer_score(payment, c), default=None)
    return best if best and payer_score(payment, best) >= floor else None


def open_balance(invoice: Invoice, paid: dict[str, int] | None = None) -> int:
    return invoice.gross_paise - invoice.credit_note_paise - (paid or {}).get(invoice.id, 0)


def tds_paise(taxable_paise: int, rate_bp: int) -> int:
    """TDS on the taxable value (excl. GST), rounded half-up to the rupee, per invoice."""
    return (taxable_paise * rate_bp + 500_000) // 1_000_000 * 100


def cited_numbers(text: str, invoices: list[Invoice]) -> set[str]:
    found = set()
    for inv in invoices:
        parts = re.findall(r"[a-z]+|\d+", inv.number.casefold())
        pattern = r"(?<![a-z0-9])" + r"[-/ ]?".join(parts) + r"(?![0-9])"
        if parts and re.search(pattern, text.casefold()):
            found.add(inv.number)
    return found


@dataclass
class MatchResult:
    outcome: str                       # reference | favoured | ambiguous | none
    candidates: list[Allocation] = field(default_factory=list)  # best first
    reason: str = ""
    lean: str | None = None            # history alone ranks the first one higher; still ambiguous
    shares: list[float] = field(default_factory=list)


def _rates(customer: Customer, tds: Tds) -> list[int]:
    if customer.tds_rate_bp is None:
        return tds.standard_rates_bp
    return [customer.tds_rate_bp] if customer.tds_rate_bp else []


def _allocation(payment, subset, balances, kind, deductions, rate=None, basis=()) -> Allocation:
    if kind == "partial":
        lines = {subset[0].id: payment.amount_paise}
    else:
        lines = {i.id: balances[i.id] - d for i, d in zip(subset, deductions, strict=True)}
        lines[subset[-1].id] += payment.amount_paise - sum(lines.values())  # absorb rounding tolerance
    return Allocation(
        lines=lines, kind=kind, tds_paise=sum(deductions), tds_rate_bp=rate,
        amount_paise=payment.amount_paise, basis=list(basis),
    )


def _exact_candidates(payment, subset, balances, customer, tds: Tds) -> list[Allocation]:
    slack = tds.tolerance_paise_per_invoice * len(subset)
    gross = sum(balances[i.id] for i in subset)
    if abs(payment.amount_paise - gross) <= slack:
        return [_allocation(payment, subset, balances, "exact_gross", [0] * len(subset))]
    out = []
    for rate in _rates(customer, tds):
        deductions = [tds_paise(i.taxable_paise, rate) for i in subset]
        if sum(deductions) and abs(payment.amount_paise - (gross - sum(deductions))) <= slack:
            note = ["rate not on file"] if customer.tds_rate_bp is None else []
            out.append(_allocation(payment, subset, balances, "tds_at_rate", deductions, rate, note))
    return out


def _history_ratio(customer: Customer) -> float:
    history = customer.payment_history
    if not history.get("n_payments"):
        return 0.5
    return history.get("tds_deducted_ratio", 0.5)


def _score(candidate, payment, by_id, customer, tds, reported_tds, reported_numbers, history) -> tuple[float, list[str]]:
    invoices = [by_id[i] for i in candidate.lines]
    amount_fit = {"exact_gross": 1.0, "tds_at_rate": 1.0, "partial": 0.5}[candidate.kind]
    if candidate.kind == "tds_at_rate" and customer.tds_rate_bp is None:
        amount_fit = 0.6
    days = sum(abs((payment.value_date - i.due_date).days) for i in invoices) / len(invoices)
    score = amount_fit * payer_score(payment, customer) / (1 + days / 180)
    # a bare invoice number in the narration is weak support, not a reference
    narration_numbers = set(re.findall(r"\d{3,}", payment.narration))
    if any(re.findall(r"\d+", i.number)[-1:] and re.findall(r"\d+", i.number)[-1] in narration_numbers
           for i in invoices):
        score *= 1.2

    why = []
    slack = tds.tolerance_paise_per_invoice * len(invoices)
    if reported_tds and candidate.kind == "tds_at_rate" and abs(candidate.tds_paise - reported_tds) <= slack:
        score *= 1.5
        why.append(f"matches the {fmt_inr(reported_tds)} TDS the buyer reported")
    if reported_numbers and {i.number for i in invoices} == reported_numbers:
        score *= 1.5
        why.append("matches the invoices listed in the buyer's report")
    fit = {"tds_at_rate": history, "exact_gross": 1 - history}.get(candidate.kind, 0.5)
    return score * (0.9 + 0.2 * fit), why


def match(
    payment: Payment,
    invoices: list[Invoice],
    customer: Customer,
    tds: Tds,
    *,
    paid: dict[str, int] | None = None,
    reported_tds_paise: int | None = None,
    reported_invoice_numbers: set[str] = frozenset(),
    remittance_invoice_numbers: set[str] = frozenset(),
) -> MatchResult:
    """`invoices` are this customer's invoices. `paid` is confirmed cash per invoice id."""
    balances = {i.id: open_balance(i, paid) for i in invoices}
    open_invoices = sorted(
        (i for i in invoices if i.customer_id == customer.id and balances[i.id] > 0),
        key=lambda i: (i.due_date, i.number),
    )

    cited = cited_numbers(payment.narration, open_invoices) or (
        set(remittance_invoice_numbers) & {i.number for i in open_invoices}
    )
    if cited:
        subset = [i for i in open_invoices if i.number in cited]
        hits = _exact_candidates(payment, subset, balances, customer, tds)
        if len(hits) == 1:
            hit = hits[0].model_copy(update={
                "confidence": REFERENCE_CONFIDENCE, "cites_invoice": True,
                "basis": [*hits[0].basis, f"cites {', '.join(sorted(cited))}"],
            })
            return MatchResult("reference", [hit], hit.basis[-1], shares=[1.0])

    # ponytail: only the 12 oldest open invoices, subsets up to 4; a smarter search if a buyer outgrows it
    pool = open_invoices[:MAX_OPEN_INVOICES]
    candidates = [
        c
        for size in range(1, MAX_SUBSET + 1)
        for subset in combinations(pool, size)
        for c in _exact_candidates(payment, list(subset), balances, customer, tds)
    ]
    if not candidates:  # partial only when nothing fits exactly, so TDS is never called a part payment
        rates = _rates(customer, tds)
        for inv in pool:
            largest_tds = max((tds_paise(inv.taxable_paise, r) for r in rates), default=0)
            floor = balances[inv.id] - largest_tds - tds.tolerance_paise_per_invoice
            if payment.amount_paise < floor and (not cited or inv.number in cited):
                candidates.append(_allocation(payment, [inv], balances, "partial", []))
    if not candidates:
        return MatchResult("none", reason="No invoice or set of invoices fits this amount.")

    by_id = {i.id: i for i in open_invoices}
    history = _history_ratio(customer)
    scored = [
        (c, *_score(c, payment, by_id, customer, tds, reported_tds_paise, set(reported_invoice_numbers), history))
        for c in candidates
    ]
    scored.sort(key=lambda s: -s[1])
    total = sum(s for _, s, _ in scored)
    shares = [s / total for _, s, _ in scored]
    ranked = [
        c.model_copy(update={
            "confidence": round(min(share, UNREFERENCED_CAP), 4),
            "cites_invoice": bool(cited) and {by_id[i].number for i in c.lines} <= cited,
            "basis": [*c.basis, *why],
        })
        for (c, _, why), share in zip(scored, shares, strict=True)
    ]

    top_why = scored[0][2]
    if len(ranked) == 1:
        return MatchResult("favoured", ranked, "; ".join(top_why) or "only candidate that fits", shares=shares)
    if shares[0] - shares[1] >= AMBIGUOUS_GAP:
        return MatchResult("favoured", ranked, "; ".join(top_why) or "scores clearly highest", shares=shares)
    lean = None
    first, second = ranked[0], ranked[1]
    if first.kind != second.kind and history != 0.5:
        if first.kind == "tds_at_rate" and history > 0.5:
            lean = f"leans: usual {first.tds_rate_bp / 100:g}% deduction"
        elif first.kind == "exact_gross" and history < 0.5:
            lean = "leans: usually pays in full"
    return MatchResult("ambiguous", ranked, "; ".join(top_why) or "candidates are too close to call", lean, shares)


@dataclass(frozen=True)
class Settlement:
    expected_paise: int
    cash_received_paise: int
    tds_reported_paise: int
    tds_evidenced_paise: int
    unexplained_paise: int
    status: str  # open | partially_settled | settled_pending_tds_evidence | settled


def settlement(
    expected_paise: int,
    cash_received_paise: int = 0,
    tds_reported_paise: int = 0,
    tds_evidenced_paise: int = 0,
    tolerance_paise: int = 100,
) -> Settlement:
    """`expected` is gross minus credit notes. There is no single 'paid' flag: settled needs nothing unexplained."""
    unexplained = expected_paise - cash_received_paise - tds_evidenced_paise
    if not cash_received_paise and not tds_evidenced_paise:
        status = "open"
    elif unexplained == 0:
        status = "settled"
    elif tds_reported_paise and 0 < unexplained <= tds_reported_paise - tds_evidenced_paise + tolerance_paise:
        status = "settled_pending_tds_evidence"
    else:
        status = "partially_settled"
    return Settlement(
        expected_paise, cash_received_paise, tds_reported_paise, tds_evidenced_paise, unexplained, status
    )
