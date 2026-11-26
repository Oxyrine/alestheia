"""The policy engine. `authorise` is pure: no I/O, no clock, no LLM."""
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict

from dlra import gates
from dlra.models import Action as A
from dlra.models import (
    Allocation,
    Contact,
    DateStatus,
    Diagnosis,
    Evidence,
    Fallback,
    Promise,
    PromiseStatus,
    State,
    Verdict,
)
from dlra.rules import ALWAYS_SAFE, OUTBOUND, RULE_TABLE, rule_text


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessHours(_Strict):
    days: list[str]
    start: str
    end: str
    tz: str


class Limits(_Strict):
    max_instalments: int
    max_tenure_days: int
    min_upfront_pct: int
    max_discount_pct: int
    forbidden_concessions: list[str]


class Tds(_Strict):
    standard_rates_bp: list[int]
    tolerance_paise_per_invoice: int


class Policy(_Strict):
    diag_threshold: float
    pay_auto_post_threshold: float
    business_hours: BusinessHours
    weekly_contact_cap: int
    silent_after_days: int
    safe_pause_days: int
    promise_grace_days: int
    reminder_window_days: int
    dispute_response_days: int
    payment_report_verify_days: int
    limits: Limits
    tds: Tds


DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.yaml"


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> Policy:
    return Policy.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class ProposedAction(_Strict):
    action: A
    text: str | None = None          # subject + body, once drafted
    facts_text: str = ""             # what the drafter was given (C-LEAK exemption)
    plan: Promise | None = None
    discount_pct: float = 0
    evidence_ids: list[str] = []     # the basis for this action


class PolicyContext(_Strict):
    state: State
    now: AwareDatetime
    policy: Policy
    diagnosis: Diagnosis
    contact: Contact | None = None   # intended recipient
    contacts: list[Contact] = []     # all of this buyer's contacts
    evidence: list[Evidence] = []
    payment_reported: bool = False   # a live (not expired) payment report
    dispute_open: bool = False
    human_pause: bool = False
    sent_this_week: int = 0
    neutral_questions_sent: int = 0
    payment_report_expired: bool = False  # and the supplier has not yet decided
    bank_ref_asks_since_expiry: int = 0
    promise: Promise | None = None
    next_due: date | None = None
    allocation: Allocation | None = None
    open_balance_paise: int = 0      # of the invoices the allocation covers
    tds_evidenced_paise: int = 0
    human_approved: bool = False


# --- lints -----------------------------------------------------------------

BANNED_TERMS = (
    "legal action", "legal notice", "legal proceedings", "lawyer", "advocate", "court", "lawsuit",
    "blacklist", "blacklisted", "cibil", "credit score", "police", "consequences", "final warning",
    "last warning", "defaulter", "recovery agent", "or else", "strict action",
)
_BANNED = re.compile(r"\b(" + "|".join(re.escape(t) for t in BANNED_TERMS) + r")\b", re.IGNORECASE)
_SHOUT_WORD = re.compile(r"\b(URGENT|IMMEDIATELY|NOW|FINAL|WARNING|MUST|OVERDUE)\b")
_SHOUT_RUN = re.compile(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,}){2,}\b")


def lint_tone(text: str) -> list[str]:
    hits = [m.group(0).lower() for m in _BANNED.finditer(text)]
    if "!" in text:
        hits.append("exclamation mark")
    if _SHOUT_WORD.search(text) or _SHOUT_RUN.search(text):
        hits.append("all-caps")
    return hits


def _limit_patterns(limits: Limits) -> dict[str, re.Pattern]:
    def pat(number: int, unit: str) -> re.Pattern:
        return re.compile(rf"(?<![\d.,]){number}[\s-]*(?:{unit})", re.IGNORECASE)

    patterns = {
        f"{limits.max_tenure_days} days": pat(limits.max_tenure_days, r"days?\b"),
        f"{limits.min_upfront_pct}%": pat(limits.min_upfront_pct, r"%|per\s?cent\b"),
        f"{limits.max_instalments} instalments": pat(
            limits.max_instalments, r"(?:equal\s+|monthly\s+)?install?ments?\b"
        ),
    }
    if limits.max_discount_pct:
        patterns[f"{limits.max_discount_pct}% discount"] = pat(limits.max_discount_pct, r"(?:%|per\s?cent)\s*discount")
    return patterns


def lint_leak(text: str, facts_text: str, limits: Limits) -> list[str]:
    """Configured limits that appear in the message but not in the facts given to the drafter."""
    return [name for name, p in _limit_patterns(limits).items() if p.search(text) and not p.search(facts_text)]


def check_limits(
    plan: Promise, limits: Limits, today: date, *, require_dates: bool, discount_pct: float = 0
) -> list[tuple[str, str]]:
    """Every client-limit violation as (rule_id, reason). Supplier-facing only."""
    out = []
    forbidden = set(limits.forbidden_concessions) | {"unknown_condition"}
    if bad := sorted(forbidden & set(plan.concessions)):
        out.append(("L-CONCESSION", f"Concession not allowed: {', '.join(bad)}."))
    if discount_pct > limits.max_discount_pct or ("discount" in plan.concessions and limits.max_discount_pct == 0):
        out.append(("L-DISCOUNT", f"Discount beyond the {limits.max_discount_pct}% limit."))
    if len(plan.installments) > limits.max_instalments:
        out.append(("L-INSTALMENTS", f"{len(plan.installments)} instalments; limit {limits.max_instalments}."))
    if plan.installments and plan.installments[0].amount_paise * 100 < limits.min_upfront_pct * plan.total_paise:
        out.append(("L-UPFRONT", f"First instalment is under {limits.min_upfront_pct}% of the total."))
    late = [
        i for i in plan.installments
        if i.date_status == DateStatus.RESOLVED and (i.due_date - today).days > limits.max_tenure_days
    ]
    if late:
        out.append(("L-TENURE", f"An instalment falls more than {limits.max_tenure_days} days out."))
    elif require_dates and (
        not plan.installments or any(i.date_status != DateStatus.RESOLVED for i in plan.installments)
    ):
        out.append(("L-TENURE", "Dates are not exact, so the tenure is unknown."))
    return out


def in_business_hours(now: datetime, hours: BusinessHours) -> bool:
    local = now.astimezone(ZoneInfo(hours.tz))
    return local.strftime("%a") in hours.days and hours.start <= local.strftime("%H:%M") < hours.end


# --- authorise -------------------------------------------------------------

_HARD_NEVER = {
    A.LEGAL_NOTICE: "H-01",
    A.LEGAL_THREAT: "H-01",
    A.SETTLEMENT_DECISION: "H-01",
    A.CREDIT_DECISION: "H-01",
    A.LEGAL_REFERRAL: "H-07",
}
_DISTRESS_SOURCES = {"crustdata", "olostep"}
_INTERNAL_SAFE = ALWAYS_SAFE | {
    A.CARD_MATERIAL_CHANGE, A.CARD_AMBIGUOUS_MATCH, A.STOP_OUTREACH, A.ARCHIVE, A.REDIAGNOSE,
}
# Permitted even when the diagnosis is weak: they change no case state.
_DIAG_EXEMPT = _INTERNAL_SAFE | {A.SUGGEST_ALLOCATION, A.NEUTRAL_QUESTION_ONCE, A.ASK_BANK_REF}
_NEEDS_ACTIVE_PROMISE = {A.TRACK_PROMISE, A.REMINDER_NEAR_DUE, A.PAYMENT_LINK}
# Governed by hard rules and limits, not by a state row.
_LEDGER_ACTIONS = {A.MARK_PAID, A.ACTIVATE_PROMISE}
# H-04 bans chasing; these are the replies that resolve the thing that caused the silence.
_REPLY_TO_REPORT = {A.ASK_BANK_REF}
_REPLY_TO_DISPUTE = {A.DISPUTE_MESSAGE, A.REQUEST_EVIDENCE}


def authorise(ctx: PolicyContext, proposed: ProposedAction) -> Verdict:
    """First failing check wins: hard rules, gates, state row, conduct, client limits, business hours."""
    for check in (_hard, _gates, _state_row, _conduct, _limits, _hours):
        verdict = check(ctx, proposed)
        if verdict is not None:
            if not verdict.evidence_ids:
                verdict.evidence_ids = list(proposed.evidence_ids or ctx.diagnosis.evidence_ids)
            return verdict
    return _allowed_verdict(ctx, proposed)


def _refuse(rule_id: str, reason: str = "", fallback: Fallback = Fallback.CARD) -> Verdict:
    return Verdict(allowed=False, rule_id=rule_id, reason=reason or rule_text(rule_id), fallback=fallback)


def _hard(ctx: PolicyContext, p: ProposedAction) -> Verdict | None:
    action = p.action
    if action in _HARD_NEVER:
        return _refuse(_HARD_NEVER[action])

    if action == A.MARK_PAID and not ctx.human_approved:
        pay = gates.g_pay(
            ctx.allocation,
            ctx.open_balance_paise,
            tds_evidenced_paise=ctx.tds_evidenced_paise,
            threshold=ctx.policy.pay_auto_post_threshold,
            tolerance_paise=ctx.policy.tds.tolerance_paise_per_invoice,
        )
        if not pay.allowed:
            return _refuse("H-03", f"{rule_text('H-03')} {pay.reason}")

    if action not in _INTERNAL_SAFE:
        basis = set(p.evidence_ids or ctx.diagnosis.evidence_ids)
        sources = {e.source_type for e in ctx.evidence if e.id in basis}
        if sources and sources <= _DISTRESS_SOURCES:
            return _refuse("H-05", "The only basis is a public distress signal.")

    if action in _NEEDS_ACTIVE_PROMISE:
        promise = ctx.promise
        if not (promise and promise.status == PromiseStatus.ACTIVE and promise.buyer_confirmed and promise.policy_ok):
            return _refuse("H-06")
    if action == A.ACTIVATE_PROMISE and not (p.plan and p.plan.buyer_confirmed):
        return _refuse("H-06", "The buyer has not confirmed this plan.")

    if action in OUTBOUND:
        if ctx.human_pause:
            return _refuse("H-04", "A human has paused contact on this case.", Fallback.SAFE_PAUSE)
        if ctx.contact and (ctx.contact.opted_out or any(c.opted_out for c in ctx.contacts)):
            return _refuse("H-04", "The buyer has opted out.", Fallback.SAFE_PAUSE)
        if ctx.payment_reported and action not in _REPLY_TO_REPORT:
            return _refuse("H-04", "The buyer has reported a payment.", Fallback.SAFE_PAUSE)
        if ctx.dispute_open and action not in _REPLY_TO_DISPUTE:
            return _refuse("H-04", "There is an open dispute on this invoice.", Fallback.SAFE_PAUSE)
        if ctx.contact is None or ctx.contact.status != "verified":
            who = f"{ctx.contact.email} is {ctx.contact.status}" if ctx.contact else "there is no contact"
            return _refuse("H-02", f"{rule_text('H-02')} Here, {who} (G-CONTACT).")
    return None


def _gates(ctx: PolicyContext, p: ProposedAction) -> Verdict | None:
    if p.action in OUTBOUND:
        contact = gates.g_contact(ctx.contact, ctx.contacts)
        if not contact.allowed:
            return contact
    if p.action not in _DIAG_EXEMPT and not ctx.human_approved:
        diag = gates.g_diag(ctx.diagnosis, ctx.evidence, ctx.policy.diag_threshold)
        if not diag.allowed:
            return diag
    return None


def _state_row(ctx: PolicyContext, p: ProposedAction) -> Verdict | None:
    action = p.action
    if action in _LEDGER_ACTIONS:
        return None
    row = RULE_TABLE[ctx.state]

    if ctx.payment_report_expired and action in OUTBOUND:
        if action != A.ASK_BANK_REF or ctx.bank_ref_asks_since_expiry >= 1:
            return _refuse("R-PAYMENT-REPORT-EXPIRED")
        if ctx.state not in (State.RESOLVED, State.DISPUTE):
            return None

    if action in ALWAYS_SAFE:
        return None
    if action in row.forbidden:
        rule = row.forbidden[action]
        return _refuse(rule.id, rule.text)
    if action not in row.allowed:
        if ctx.state == State.RESOLVED and action in OUTBOUND:
            rule = row.forbidden[A.ANY_SEQUENCE]
            return _refuse(rule.id, rule.text, Fallback.ABSTAIN)
        return _refuse(f"S-{ctx.state}-DEFAULT-DENY")

    if action == A.NEUTRAL_QUESTION_ONCE and ctx.neutral_questions_sent >= 1:
        rule = row.forbidden[A.REPEATED_OUTREACH]
        return _refuse(rule.id, rule.text, Fallback.SAFE_PAUSE)
    if action == A.REMINDER_NEAR_DUE:
        days = None if ctx.next_due is None else (ctx.next_due - ctx.now.date()).days
        if days is None or not 0 <= days <= ctx.policy.reminder_window_days:
            return _refuse(row.allowed[action].id, "Reminders go out only near the promised date.", Fallback.ABSTAIN)
    return None


def _conduct(ctx: PolicyContext, p: ProposedAction) -> Verdict | None:
    if p.text:
        if hits := lint_tone(p.text):
            return _refuse("C-TONE", f"Threatening or shouting language: {', '.join(hits)}.")
        if p.action in OUTBOUND and (leaks := lint_leak(p.text, p.facts_text, ctx.policy.limits)):
            return _refuse("C-LEAK", f"Message reveals a supplier limit: {', '.join(leaks)}.")
    if p.action in OUTBOUND and ctx.sent_this_week >= ctx.policy.weekly_contact_cap:
        return _refuse(
            "C-CAP", f"Weekly contact cap of {ctx.policy.weekly_contact_cap} reached.", Fallback.SAFE_PAUSE
        )
    return None


def _limits(ctx: PolicyContext, p: ProposedAction) -> Verdict | None:
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.business_hours.tz)).date()
    if p.action in (A.PROPOSE_PLAN, A.ACTIVATE_PROMISE):
        if p.plan is None:
            return _refuse("L-TENURE", "No plan to check.")
        violations = check_limits(
            p.plan, ctx.policy.limits, today,
            require_dates=p.action == A.ACTIVATE_PROMISE, discount_pct=p.discount_pct,
        )
        if violations:
            return _refuse(violations[0][0], " ".join(reason for _, reason in violations))
    elif p.discount_pct > ctx.policy.limits.max_discount_pct:
        return _refuse("L-DISCOUNT")
    return None


def _hours(ctx: PolicyContext, p: ProposedAction) -> Verdict | None:
    # Last on purpose: queueing must never hide a refusal.
    if p.action in OUTBOUND and not in_business_hours(ctx.now, ctx.policy.business_hours):
        return _refuse("C-HOURS", "Outside business hours: queued.", Fallback.QUEUE)
    return None


def _allowed_verdict(ctx: PolicyContext, p: ProposedAction) -> Verdict:
    rule = RULE_TABLE[ctx.state].allowed.get(p.action)
    if rule:
        rule_id = rule.id
    elif p.action in ALWAYS_SAFE:
        rule_id = "S-ALWAYS-SAFE"
    elif ctx.payment_report_expired and p.action == A.ASK_BANK_REF:
        rule_id = "R-PAYMENT-REPORT-EXPIRED"
    else:
        rule_id = None  # ledger actions, cleared by hard rules and limits
    return Verdict(
        allowed=True, rule_id=rule_id, reason=rule_text(rule_id) if rule_id else "",
        evidence_ids=list(p.evidence_ids or ctx.diagnosis.evidence_ids),
    )
