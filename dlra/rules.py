"""The rule table. One data structure: the engine enforces it and the UI renders it."""
from dataclasses import dataclass

from dlra.models import Action as A
from dlra.models import State as S


@dataclass(frozen=True)
class Rule:
    id: str
    action: A
    text: str


@dataclass(frozen=True)
class Row:
    label: str
    spec: str  # the spec's rule-table row this encodes
    allowed: dict[A, Rule]
    forbidden: dict[A, Rule]


def _row(state: S, label: str, spec: str, allowed: dict, forbidden: dict) -> Row:
    def cells(kind: str, items: dict) -> dict[A, Rule]:
        return {a: Rule(f"S-{state}-{kind}{n}", a, text) for n, (a, text) in enumerate(items.items(), 1)}

    return Row(label, spec, cells("A", allowed), cells("F", forbidden))


# Order matters: rule IDs are numbered by position. Append, never reorder.
RULE_TABLE: dict[S, Row] = {
    S.PAPERWORK: _row(
        S.PAPERWORK, "Paperwork problem",
        "Draft the correction; request one missing item; supplier approves any material change or reissued "
        "invoice | Escalation, discount",
        {A.DRAFT_CORRECTION: "Draft the corrected document.",
         A.REQUEST_ONE_ITEM: "Request one missing item.",
         A.CARD_MATERIAL_CHANGE: "The supplier approves any material change or reissued invoice."},
        {A.ESCALATION: "A paperwork problem is never escalated.",
         A.DISCOUNT: "No discount for a paperwork problem."},
    ),
    S.DISPUTE: _row(
        S.DISPUTE, "Dispute",
        "Stop reminders; dispute-resolution messages; request evidence; supplier card | Payment reminders, "
        "settlement offers",
        {A.STOP_REMINDERS: "Stop all payment reminders.",
         A.DISPUTE_MESSAGE: "Send dispute-resolution messages.",
         A.REQUEST_EVIDENCE: "Request evidence for the dispute.",
         A.CARD_SUPPLIER: "Raise a supplier card."},
        {A.PAYMENT_REMINDER: "No payment reminders while an invoice is disputed.",
         A.SETTLEMENT_OFFER: "No settlement offers on a disputed invoice."},
    ),
    S.CASH_CONSTRAINT: _row(
        S.CASH_CONSTRAINT, "Cash constraint",
        "Propose a plan within client limits, held as proposed until the buyer confirms | More frequent "
        "contact, discount beyond limit",
        {A.PROPOSE_PLAN: "Propose a plan within client limits, held until the buyer confirms.",
         A.CONFIRMATION_REQUEST: "Ask the buyer to confirm exact dates.",
         A.DECLINE_CONDITION: "Decline an out-of-policy condition without revealing the limit."},
        {A.INCREASE_CONTACT: "No more frequent contact.",
         A.DISCOUNT_BEYOND_LIMIT: "No discount beyond the client limit."},
    ),
    S.PROMISE_ACTIVE: _row(
        S.PROMISE_ACTIVE, "Promise active",
        "Track exact dates, amounts and invoice allocation; remind only near the promised date | Escalation",
        {A.TRACK_PROMISE: "Track exact dates, amounts and invoice allocation.",
         A.REMINDER_NEAR_DUE: "Remind only near the promised date.",
         A.PAYMENT_LINK: "Offer a test-mode payment link for a promised instalment."},
        {A.ESCALATION: "No escalation while a promise is active."},
    ),
    S.PROMISE_BROKEN: _row(
        S.PROMISE_BROKEN, "Promise broken",
        "Re-diagnose; human card | An automatic harsher message",
        {A.REDIAGNOSE: "Re-diagnose the case.",
         A.CARD_SUPPLIER: "Raise a human card."},
        {A.HARSHER_MESSAGE: "A broken promise never triggers an automatic harsher message."},
    ),
    S.PAID_UNMATCHED: _row(
        S.PAID_UNMATCHED, "Paid but unmatched",
        "Suggest candidate allocations; ask for the bank reference; card if ambiguous | Payment reminders; "
        "posting an ambiguous match",
        {A.SUGGEST_ALLOCATION: "Suggest candidate allocations.",
         A.ASK_BANK_REF: "Ask for the bank reference.",
         A.CARD_AMBIGUOUS_MATCH: "Raise a card if the match is ambiguous."},
        {A.PAYMENT_REMINDER: "No payment reminders once a payment may have been made.",
         A.POST_AMBIGUOUS_MATCH: "Never post an ambiguous match."},
    ),
    S.SILENT_UNKNOWN: _row(
        S.SILENT_UNKNOWN, "Silent or unknown",
        "One neutral question, only to a verified contact; safe pause | Repeated outreach, escalation",
        {A.NEUTRAL_QUESTION_ONCE: "Ask one neutral question, only to a verified contact.",
         A.SAFE_PAUSE: "Pause safely."},
        {A.REPEATED_OUTREACH: "No repeated outreach to a silent buyer.",
         A.ESCALATION: "Silence never unlocks escalation."},
    ),
    S.RESOLVED: _row(
        S.RESOLVED, "Resolved",
        "Stop outreach; archive the case | Any further sequence",
        {A.STOP_OUTREACH: "Stop outreach.",
         A.ARCHIVE: "Archive the case."},
        {A.ANY_SEQUENCE: "No further sequence on a resolved case."},
    ),
}

# Routing to a human or doing less is permitted in every state.
ALWAYS_SAFE = frozenset({A.CARD_SUPPLIER, A.SAFE_PAUSE, A.STOP_REMINDERS})

# Anything that reaches the buyer. Gates, H-04, C-CAP and C-HOURS apply to these.
OUTBOUND = frozenset({
    A.REQUEST_ONE_ITEM, A.DISPUTE_MESSAGE, A.REQUEST_EVIDENCE, A.CONFIRMATION_REQUEST, A.DECLINE_CONDITION,
    A.REMINDER_NEAR_DUE, A.PAYMENT_LINK, A.ASK_BANK_REF, A.NEUTRAL_QUESTION_ONCE, A.PAYMENT_REMINDER,
    A.ESCALATION, A.HARSHER_MESSAGE, A.SETTLEMENT_OFFER, A.INCREASE_CONTACT, A.REPEATED_OUTREACH,
    A.ANY_SEQUENCE,
})

HARD_RULES = {
    "H-01": "No automatic legal notice, legal threat, settlement or credit decision.",
    "H-02": "No message to a low-confidence or conflicting contact.",
    "H-03": "No invoice marked paid on ambiguous evidence.",
    "H-04": "No contact after opt-out, a reported payment, an open dispute or a human pause.",
    "H-05": "Distress signals never prove inability or unwillingness to pay.",
    "H-06": "A payment promise is not binding until the buyer confirms and policy accepts it.",
    "H-07": "Formal notice and legal referral always need human approval.",
}

GATE_RULES = {
    "G-CONTACT": "Contact identity: no outbound to an unverified or conflicting contact.",
    "G-DIAG": "Diagnosis: low confidence goes to a human review card.",
    "G-PAY": "Payment match: low confidence means suggest only, never post.",
}

CONDUCT_RULES = {
    "C-HOURS": "Messages go out in business hours only.",
    "C-CAP": "Weekly contact cap per buyer.",
    "C-TONE": "No threatening language.",
    "C-LEAK": "Messages never reveal the supplier's limits.",
    "C-NOCONTACT": "No contact after a reported payment, an open dispute or a human pause (see H-04).",
    "C-HUMAN": "Formal notice drafts go to a human card.",
}

LIMIT_RULES = {
    "L-INSTALMENTS": "Plan exceeds the allowed number of instalments.",
    "L-TENURE": "Plan runs past the allowed tenure, or its dates are not exact.",
    "L-UPFRONT": "First instalment is below the minimum upfront share.",
    "L-DISCOUNT": "Discount is beyond the limit.",
    "L-CONCESSION": "Plan asks for a concession the supplier does not allow.",
}

OTHER_RULES = {
    "R-PAYMENT-REPORT-EXPIRED": "A reported payment never arrived: one bank-reference request only, until "
                                "the supplier decides.",
    "R-DISTRESS-REVIEW": "A distress signal against a good payment history goes to human review.",
    "S-ALWAYS-SAFE": "Pausing, stopping reminders and asking the supplier are always permitted.",
}


def rule_text(rule_id: str) -> str:
    for table in (HARD_RULES, GATE_RULES, CONDUCT_RULES, LIMIT_RULES, OTHER_RULES):
        if rule_id in table:
            return table[rule_id]
    for row in RULE_TABLE.values():
        for rule in (*row.allowed.values(), *row.forbidden.values()):
            if rule.id == rule_id:
                return rule.text
    if rule_id.endswith("-DEFAULT-DENY"):
        return "Not on this state's allowed list, so refused."
    raise KeyError(rule_id)


def check_transition(
    prev: S | None,
    new: S,
    *,
    human_reopen: bool = False,
    settled: bool = False,
    promise_active: bool = False,
    deadline_missed: bool = False,
) -> tuple[bool, str | None, str]:
    """(allowed, rule_id, reason). `deadline_missed`: a deadline fired and reconciliation found no payment."""
    if prev == new:
        return True, None, ""
    if prev == S.RESOLVED and not human_reopen:
        return False, "T-RESOLVED-TERMINAL", "A resolved case stays resolved unless a human reopens it."
    if new == S.RESOLVED and not settled:
        return False, "T-RESOLVED-SETTLED", (
            "A case resolves only through confirmed allocations with nothing unexplained."
        )
    if new == S.PROMISE_ACTIVE and not promise_active:
        return False, "T-PROMISE-ACTIVE", "A promise is active only after buyer confirmation and policy acceptance."
    if new == S.PROMISE_BROKEN and prev is not None and not (prev == S.PROMISE_ACTIVE and deadline_missed):
        return False, "T-PROMISE-BROKEN", "Only an active promise can break, and only on a missed deadline."
    return True, None, ""
