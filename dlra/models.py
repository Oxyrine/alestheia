"""Shared interfaces. Lanes B and C import these: change them only with the team.

Money is integer paise everywhere.
"""
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class State(StrEnum):
    PAPERWORK = "PAPERWORK"
    DISPUTE = "DISPUTE"
    CASH_CONSTRAINT = "CASH_CONSTRAINT"
    PROMISE_ACTIVE = "PROMISE_ACTIVE"
    PROMISE_BROKEN = "PROMISE_BROKEN"
    PAID_UNMATCHED = "PAID_UNMATCHED"
    SILENT_UNKNOWN = "SILENT_UNKNOWN"
    RESOLVED = "RESOLVED"


class Action(StrEnum):
    # allowed somewhere in the rule table
    DRAFT_CORRECTION = "DRAFT_CORRECTION"
    REQUEST_ONE_ITEM = "REQUEST_ONE_ITEM"
    CARD_MATERIAL_CHANGE = "CARD_MATERIAL_CHANGE"
    STOP_REMINDERS = "STOP_REMINDERS"
    DISPUTE_MESSAGE = "DISPUTE_MESSAGE"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    CARD_SUPPLIER = "CARD_SUPPLIER"
    PROPOSE_PLAN = "PROPOSE_PLAN"
    CONFIRMATION_REQUEST = "CONFIRMATION_REQUEST"
    DECLINE_CONDITION = "DECLINE_CONDITION"
    TRACK_PROMISE = "TRACK_PROMISE"
    REMINDER_NEAR_DUE = "REMINDER_NEAR_DUE"
    PAYMENT_LINK = "PAYMENT_LINK"
    REDIAGNOSE = "REDIAGNOSE"
    SUGGEST_ALLOCATION = "SUGGEST_ALLOCATION"
    ASK_BANK_REF = "ASK_BANK_REF"
    CARD_AMBIGUOUS_MATCH = "CARD_AMBIGUOUS_MATCH"
    NEUTRAL_QUESTION_ONCE = "NEUTRAL_QUESTION_ONCE"
    SAFE_PAUSE = "SAFE_PAUSE"
    STOP_OUTREACH = "STOP_OUTREACH"
    ARCHIVE = "ARCHIVE"
    # named as forbidden in the rule table
    ESCALATION = "ESCALATION"
    DISCOUNT = "DISCOUNT"
    PAYMENT_REMINDER = "PAYMENT_REMINDER"
    SETTLEMENT_OFFER = "SETTLEMENT_OFFER"
    INCREASE_CONTACT = "INCREASE_CONTACT"
    DISCOUNT_BEYOND_LIMIT = "DISCOUNT_BEYOND_LIMIT"
    HARSHER_MESSAGE = "HARSHER_MESSAGE"
    POST_AMBIGUOUS_MATCH = "POST_AMBIGUOUS_MATCH"
    REPEATED_OUTREACH = "REPEATED_OUTREACH"
    ANY_SEQUENCE = "ANY_SEQUENCE"
    # hard boundaries and ledger writes
    LEGAL_NOTICE = "LEGAL_NOTICE"
    LEGAL_THREAT = "LEGAL_THREAT"
    LEGAL_REFERRAL = "LEGAL_REFERRAL"
    SETTLEMENT_DECISION = "SETTLEMENT_DECISION"
    CREDIT_DECISION = "CREDIT_DECISION"
    MARK_PAID = "MARK_PAID"
    ACTIVATE_PROMISE = "ACTIVATE_PROMISE"


class Outcome(StrEnum):
    ACT = "ACT"          # message queued or document drafted
    PROPOSE = "PROPOSE"  # promise or allocation held as proposed
    CARD = "CARD"        # human card raised
    ABSTAIN = "ABSTAIN"  # safe pause, no outbound


class Fallback(StrEnum):
    CARD = "CARD"
    SAFE_PAUSE = "SAFE_PAUSE"
    QUEUE = "QUEUE"
    ABSTAIN = "ABSTAIN"


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    CONFLICTING = "conflicting"
    EXPIRED_UNVERIFIED = "expired_unverified"


class DateStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class PromiseStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ACTIVE = "active"
    BROKEN = "broken"
    FULFILLED = "fulfilled"
    DECLINED = "declined"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Model):
    id: str
    case_id: str
    source_type: str  # ledger | pdf | email | bank | buyer_page | human | crustdata | olostep
    source_ref: str
    ts: datetime
    field: str
    claim: str
    value: object = None
    confidence: float = 1.0
    status: EvidenceStatus = EvidenceStatus.UNVERIFIED
    influenced_decision_id: str | None = None


class Contact(_Model):
    id: str
    customer_id: str
    name: str = ""
    email: str
    source: str = "ledger"
    source_evidence_id: str | None = None
    status: EvidenceStatus = EvidenceStatus.UNVERIFIED  # verified | unverified | conflicting
    opted_out: bool = False


class Diagnosis(_Model):
    state: State
    confidence: float = Field(ge=0, le=1)  # computed in code, never the LLM's own number
    evidence_ids: list[str] = []
    rationale: str = ""
    signals: list[str] = []
    llm_confidence: float | None = None


class Verdict(_Model):
    allowed: bool
    rule_id: str | None = None
    reason: str = ""
    fallback: Fallback | None = None
    evidence_ids: list[str] = []


class Installment(_Model):
    amount_paise: int
    due_text: str
    due_date: date | None = None
    date_status: DateStatus = DateStatus.UNRESOLVED


class Promise(_Model):
    id: str
    invoice_ids: list[str]
    installments: list[Installment]
    conditions: list[str] = []         # buyer's wording
    concessions: list[str] = []        # mapped codes, e.g. late_fee_waiver
    status: PromiseStatus = PromiseStatus.PROPOSED
    buyer_confirmed: bool = False
    policy_ok: bool = False
    source_evidence_id: str | None = None

    @property
    def total_paise(self) -> int:
        return sum(i.amount_paise for i in self.installments)


class Allocation(_Model):
    lines: dict[str, int]              # invoice_id -> cash allocated, paise
    kind: str                          # exact_gross | tds_at_rate | partial
    tds_paise: int = 0                 # TDS this candidate implies
    tds_rate_bp: int | None = None
    amount_paise: int
    confidence: float = 0.0
    cites_invoice: bool = False        # narration or remittance note names the invoices
    basis: list[str] = []


class Invoice(_Model):
    id: str
    number: str
    customer_id: str
    po_number: str | None = None
    issue_date: date
    due_date: date
    accepted_date: date | None = None
    taxable_paise: int                 # TDS base
    gst_paise: int
    gross_paise: int                   # taxable + GST
    credit_note_paise: int = 0


class Customer(_Model):
    id: str
    canonical_name: str
    gstin: str | None = None
    entity_type: str = "company"
    aliases: list[str] = []
    tds_rate_bp: int | None = None     # None = not on file, 0 = does not deduct
    tds_basis: str = "none"
    payment_history: dict[str, float] = {}  # on_time_ratio, avg_days_late, tds_deducted_ratio, n_payments


class Payment(_Model):
    id: str
    amount_paise: int
    value_date: date
    utr: str | None = None
    narration: str = ""
    payer_name: str = ""
    source: str = "bank"


class CaseBundle(_Model):
    """One case as the engine sees it. Eval case JSON uses this same shape.

    `invoice` is the case's invoice. `open_invoices` is every open invoice of the
    same customer (including this one), for multi-invoice matching.
    """
    case_id: str
    invoice: Invoice
    customer: Customer
    open_invoices: list[Invoice] = []
    contacts: list[Contact] = []
    evidence: list[Evidence] = []
    payments: list[Payment] = []
    promise: Promise | None = None
    state: State | None = None         # previous state, None for a new case


def fmt_inr(paise: int) -> str:
    """Indian digit grouping: ₹4,44,860 (paise shown only when non-zero)."""
    sign, paise = ("-" if paise < 0 else ""), abs(paise)
    rupees, rest = divmod(paise, 100)
    digits = str(rupees)
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while head:
        head, groups = head[:-2], [head[-2:], *groups]
    return f"{sign}₹{','.join([*groups, tail])}" + (f".{rest:02d}" if rest else "")
