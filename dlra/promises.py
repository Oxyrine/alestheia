"""Payment promises. The LLM extracts text; code computes amounts, resolves dates and decides."""
import re
from datetime import date

from dlra.models import DateStatus, Installment, Promise, PromiseStatus
from dlra.policy import Limits, check_limits

# Mapped from the buyer's own words. Anything unmatched is "unknown_condition", which policy refuses.
CONDITION_PATTERNS = {
    "late_fee_waiver": r"\blate[\s-]*(payment\s+)?(fee|fees|charge|charges)\b|\bpenalt(y|ies)\b",
    "interest_waiver": r"\binterest\b",
    "credit_note_without_approval": r"\bcredit\s*note\b",
    "discount": r"\bdiscount\b|\brebate\b",
}
KNOWN_CODES = frozenset(CONDITION_PATTERNS)

_MONTHS = {m: n for n, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_DATE_FORMS = [
    re.compile(r"(?P<year>\d{4})-(?P<num>\d{2})-(?P<day>\d{2})"),
    re.compile(r"(?P<day>\d{1,2})[/.-](?P<num>\d{1,2})[/.-](?P<year>\d{4}|\d{2})"),  # dd/mm/yyyy
    re.compile(rf"{_DAY}[\s-]+{_MONTH}\.?(?:[\s,-]+(?P<year>\d{{4}}|\d{{2}}))?"),
    re.compile(rf"{_MONTH}\.?[\s-]+{_DAY}(?:[\s,]+(?P<year>\d{{4}}))?"),
]


def parse_due(text: str, today: date) -> date | None:
    """Only a complete absolute date resolves. 'next week' and 'after 15 Dec' never do."""
    cleaned = re.sub(r"^(by|on)\s+", "", text.strip().casefold().rstrip("."))
    for form in _DATE_FORMS:
        m = form.fullmatch(cleaned)
        if not m:
            continue
        parts = m.groupdict()
        month = int(parts["num"]) if parts.get("num") else _MONTHS[parts["mon"][:3]]
        year = int(parts["year"]) if parts.get("year") else today.year
        year += 2000 if year < 100 else 0
        try:
            due = date(year, month, int(parts["day"]))
            if due < today and not parts.get("year"):
                due = due.replace(year=year + 1)
        except ValueError:
            return None
        return due if due >= today else None
    return None


def map_condition(text: str, llm_code: str | None = None) -> list[str]:
    """The LLM tag can add a code but never remove one the synonym table found."""
    codes = {code for code, pattern in CONDITION_PATTERNS.items() if re.search(pattern, text, re.IGNORECASE)}
    if llm_code in KNOWN_CODES:
        codes.add(llm_code)
    return sorted(codes) or ["unknown_condition"]


def _installment(amount: int, due_text: str, today: date) -> Installment:
    due = parse_due(due_text, today)
    status = DateStatus.RESOLVED if due else DateStatus.UNRESOLVED
    return Installment(amount_paise=amount, due_text=due_text, due_date=due, date_status=status)


def build_promise(
    extracted: dict, outstanding_paise: int, today: date, promise_id: str, evidence_id: str | None = None
) -> Promise:
    """`extracted`: {invoice_ids, installments: [{amount_paise? | share?, due_text}],
    conditions: [str | {text, code}]}. Raises ValueError if the amounts make no sense."""
    parts = extracted.get("installments") or []
    if not parts:
        raise ValueError("a promise needs at least one instalment")
    amounts = []
    for index, part in enumerate(parts):
        left = outstanding_paise - sum(amounts)
        if part.get("amount_paise") is not None:
            amount = int(part["amount_paise"])
        elif index == len(parts) - 1:
            amount = left  # the last instalment takes the remainder, so the sum is exact
        elif part.get("share") is not None:
            amount = round(outstanding_paise * part["share"] / 100) * 100
        else:
            raise ValueError("only the last instalment may leave its amount unstated")
        if amount <= 0 or amount > left:
            raise ValueError("instalment amounts do not fit the outstanding balance")
        amounts.append(amount)

    conditions, concessions = [], set()
    for item in extracted.get("conditions") or []:
        text, code = (item, None) if isinstance(item, str) else (item["text"], item.get("code"))
        conditions.append(text)
        concessions.update(map_condition(text, code))
    return Promise(
        id=promise_id,
        invoice_ids=extracted["invoice_ids"],
        installments=[_installment(a, p["due_text"], today) for a, p in zip(amounts, parts, strict=True)],
        conditions=conditions,
        concessions=sorted(concessions),
        source_evidence_id=evidence_id,
    )


def spec_json(promise: Promise) -> dict:
    """The shape shown in the spec: rupees, the buyer's date words, status."""
    return {
        "promise_id": promise.id,
        "invoice_ids": promise.invoice_ids,
        "installments": [
            {"amount": i.amount_paise // 100, "due": i.due_text, "date_status": str(i.date_status)}
            for i in promise.installments
        ],
        "conditions": promise.conditions,
        "status": str(promise.status),
    }


def request_confirmation(promise: Promise) -> Promise:
    if promise.status not in (PromiseStatus.PROPOSED, PromiseStatus.AWAITING_CONFIRMATION):
        raise ValueError(f"cannot ask to confirm a {promise.status} promise")
    return promise.model_copy(update={"status": PromiseStatus.AWAITING_CONFIRMATION})


def confirm(
    promise: Promise,
    due_texts: list[str],
    limits: Limits,
    today: date,
    *,
    conditions: list[str] = (),
    approved_concessions: set[str] = frozenset(),
) -> tuple[Promise, list[tuple[str, str]]]:
    """The buyer's confirming reply. Its conditions replace the old ones.

    Active only when every date is exact and policy accepts. `approved_concessions`
    comes only from a supplier's human decision.
    """
    if promise.status != PromiseStatus.AWAITING_CONFIRMATION:
        raise ValueError(f"cannot confirm a {promise.status} promise")
    if len(due_texts) != len(promise.installments):
        raise ValueError("one date per instalment")
    dated = promise.model_copy(update={
        "installments": [
            _installment(i.amount_paise, text, today)
            for i, text in zip(promise.installments, due_texts, strict=True)
        ],
        "conditions": list(conditions),
        "concessions": sorted({code for c in conditions for code in map_condition(c)}),
        "buyer_confirmed": True,
    })
    checked = dated.model_copy(update={
        "concessions": sorted(set(dated.concessions) - set(approved_concessions))
    })
    violations = check_limits(checked, limits, today, require_dates=True)
    update = {"policy_ok": not violations}
    if not violations:
        update["status"] = PromiseStatus.ACTIVE
    return dated.model_copy(update=update), violations


def on_deadline(promise: Promise, paid_paise: int, today: date, grace_days: int) -> Promise:
    """Call after reconciliation. `paid_paise` is confirmed cash received since activation."""
    if promise.status != PromiseStatus.ACTIVE:
        return promise
    if paid_paise >= promise.total_paise:
        return promise.model_copy(update={"status": PromiseStatus.FULFILLED})
    owed = sum(i.amount_paise for i in promise.installments if (today - i.due_date).days > grace_days)
    if paid_paise < owed:
        return promise.model_copy(update={"status": PromiseStatus.BROKEN})
    return promise


def next_due(promise: Promise, paid_paise: int) -> date | None:
    covered = 0
    for item in promise.installments:
        covered += item.amount_paise
        if paid_paise < covered:
            return item.due_date
    return None
