from datetime import date, timedelta

import pytest
from conftest import NOW, contact, evidence

from dlra.models import Action as A
from dlra.models import Allocation, Fallback, Installment, Promise, State
from dlra.policy import ProposedAction, authorise, lint_leak, lint_tone
from dlra.rules import HARD_RULES, RULE_TABLE, rule_text


def run(ctx, action, **kw):
    return authorise(ctx, ProposedAction(action=action, **kw))


def plan(*parts, concessions=()):
    """parts: (rupees, due_date or None)"""
    return Promise(
        id="PRM-1", invoice_ids=["INV-882"], concessions=list(concessions),
        installments=[
            Installment(
                amount_paise=r * 100, due_text=str(d), due_date=d,
                date_status="resolved" if d else "unresolved",
            )
            for r, d in parts
        ],
    )


def active_promise():
    p = plan((100, date(2026, 12, 1)))
    return p.model_copy(update={"status": "active", "buyer_confirmed": True, "policy_ok": True})


@pytest.mark.parametrize(
    "state,action,rule",
    [(s, a, r) for s, row in RULE_TABLE.items() for a, r in row.forbidden.items()],
)
def test_every_forbidden_cell_refuses_with_its_id(make_ctx, state, action, rule):
    verdict = run(make_ctx(state), action)
    assert not verdict.allowed
    assert verdict.rule_id == rule.id
    assert verdict.fallback and rule_text(rule.id)


def test_rule_ids_the_demo_cites():
    assert RULE_TABLE[State.DISPUTE].forbidden[A.PAYMENT_REMINDER].id == "S-DISPUTE-F1"
    assert RULE_TABLE[State.PAPERWORK].forbidden[A.ESCALATION].id == "S-PAPERWORK-F1"
    assert RULE_TABLE[State.PROMISE_BROKEN].forbidden[A.HARSHER_MESSAGE].id == "S-PROMISE_BROKEN-F1"


def test_default_deny(make_ctx):
    verdict = run(make_ctx(State.PAPERWORK), A.PAYMENT_REMINDER)
    assert not verdict.allowed and verdict.rule_id == "S-PAPERWORK-DEFAULT-DENY"


def test_allowed_cell_passes_and_names_its_rule(make_ctx):
    verdict = run(make_ctx(State.PAPERWORK), A.REQUEST_ONE_ITEM)
    assert verdict.allowed and verdict.rule_id == "S-PAPERWORK-A2" and verdict.evidence_ids == ["E1"]


@pytest.mark.parametrize(
    "action,rule",
    [(A.LEGAL_NOTICE, "H-01"), (A.LEGAL_THREAT, "H-01"), (A.SETTLEMENT_DECISION, "H-01"),
     (A.CREDIT_DECISION, "H-01"), (A.LEGAL_REFERRAL, "H-07")],
)
@pytest.mark.parametrize("state", list(State))
def test_legal_actions_are_always_a_card_even_if_approved(make_ctx, state, action, rule):
    verdict = run(make_ctx(state, human_approved=True), action)
    assert (verdict.allowed, verdict.rule_id, verdict.fallback) == (False, rule, Fallback.CARD)


@pytest.mark.parametrize("status", ["unverified", "conflicting"])
def test_h02_unverified_contact(make_ctx, status):
    ctx = make_ctx(State.SILENT_UNKNOWN, contact=contact(status=status))
    assert run(ctx, A.NEUTRAL_QUESTION_ONCE).rule_id == "H-02"


def test_h02_no_contact(make_ctx):
    assert run(make_ctx(State.SILENT_UNKNOWN, contact=None), A.NEUTRAL_QUESTION_ONCE).rule_id == "H-02"


def test_g_contact_blocks_verified_target_when_another_address_conflicts(make_ctx):
    target = contact()
    other = contact("new@elsewhere.example", status="conflicting")
    ctx = make_ctx(State.SILENT_UNKNOWN, contact=target, contacts=[target, other])
    assert run(ctx, A.NEUTRAL_QUESTION_ONCE).rule_id == "G-CONTACT"


def test_h03_ambiguous_match_never_marks_paid(make_ctx):
    weak = Allocation(lines={"I1": 100}, kind="exact_gross", amount_paise=100, confidence=0.6)
    ctx = make_ctx(State.PAID_UNMATCHED, allocation=weak, open_balance_paise=100)
    assert run(ctx, A.MARK_PAID).rule_id == "H-03"
    good = weak.model_copy(update={"confidence": 0.98, "cites_invoice": True})
    assert run(ctx.model_copy(update={"allocation": good}), A.MARK_PAID).allowed
    assert run(ctx.model_copy(update={"human_approved": True}), A.MARK_PAID).allowed


@pytest.mark.parametrize("flag", ["human_pause", "payment_reported", "dispute_open"])
def test_h04_blocks_a_reminder(make_ctx, flag):
    verdict = run(make_ctx(State.DISPUTE, **{flag: True}), A.PAYMENT_REMINDER)
    assert verdict.rule_id == "H-04"


def test_h04_opt_out_blocks_everything(make_ctx):
    ctx = make_ctx(State.DISPUTE, contact=contact(opted_out=True))
    assert run(ctx, A.DISPUTE_MESSAGE).rule_id == "H-04"


def test_h04_permits_only_the_reply_that_resolves_it(make_ctx):
    reported = make_ctx(State.PAID_UNMATCHED, payment_reported=True)
    assert run(reported, A.ASK_BANK_REF).allowed
    disputed = make_ctx(State.DISPUTE, dispute_open=True)
    assert run(disputed, A.DISPUTE_MESSAGE).allowed
    both = make_ctx(State.DISPUTE, dispute_open=True, payment_reported=True)
    assert run(both, A.DISPUTE_MESSAGE).rule_id == "H-04"
    paused = make_ctx(State.PAID_UNMATCHED, human_pause=True)
    assert run(paused, A.ASK_BANK_REF).rule_id == "H-04"


def test_h05_distress_alone_is_no_basis(make_ctx):
    signal = evidence("D1", source_type="crustdata", status="unverified")
    ctx = make_ctx(State.CASH_CONSTRAINT, evidence=[signal, evidence()])
    assert run(ctx, A.CONFIRMATION_REQUEST, evidence_ids=["D1"]).rule_id == "H-05"
    assert run(ctx, A.CONFIRMATION_REQUEST, evidence_ids=["D1", "E1"]).allowed
    assert run(ctx, A.CARD_SUPPLIER, evidence_ids=["D1"]).allowed


def test_h06_no_tracking_or_payment_link_without_a_binding_promise(make_ctx):
    ctx = make_ctx(State.PROMISE_ACTIVE)
    for action in (A.TRACK_PROMISE, A.PAYMENT_LINK, A.REMINDER_NEAR_DUE):
        assert run(ctx, action).rule_id == "H-06"
    assert run(ctx.model_copy(update={"promise": active_promise()}), A.PAYMENT_LINK).allowed
    assert run(make_ctx(State.CASH_CONSTRAINT), A.ACTIVATE_PROMISE, plan=plan((100, NOW.date()))).rule_id == "H-06"


def test_reminder_only_near_due(make_ctx):
    base = dict(promise=active_promise())
    near = make_ctx(State.PROMISE_ACTIVE, next_due=NOW.date() + timedelta(days=2), **base)
    far = make_ctx(State.PROMISE_ACTIVE, next_due=NOW.date() + timedelta(days=9), **base)
    assert run(near, A.REMINDER_NEAR_DUE).allowed
    assert run(far, A.REMINDER_NEAR_DUE).rule_id == "S-PROMISE_ACTIVE-A2"


def test_h07_is_listed(make_ctx):
    assert set(HARD_RULES) == {f"H-0{n}" for n in range(1, 8)}


def test_g_diag_blocks_state_changing_actions_but_not_one_neutral_question(make_ctx):
    assert run(make_ctx(State.PAPERWORK, confidence=0.6), A.DRAFT_CORRECTION).rule_id == "G-DIAG"
    weak = make_ctx(State.SILENT_UNKNOWN, confidence=0.5)
    assert run(weak, A.NEUTRAL_QUESTION_ONCE).allowed
    assert run(weak, A.SAFE_PAUSE).allowed


def test_neutral_question_only_once(make_ctx):
    verdict = run(make_ctx(State.SILENT_UNKNOWN, neutral_questions_sent=1), A.NEUTRAL_QUESTION_ONCE)
    assert verdict.rule_id == "S-SILENT_UNKNOWN-F1"


def test_expired_payment_report_allows_exactly_one_bank_ref_ask(make_ctx):
    ctx = make_ctx(State.SILENT_UNKNOWN, payment_report_expired=True)
    first = run(ctx, A.ASK_BANK_REF)
    assert first.allowed and first.rule_id == "R-PAYMENT-REPORT-EXPIRED"
    assert run(ctx, A.NEUTRAL_QUESTION_ONCE).rule_id == "R-PAYMENT-REPORT-EXPIRED"
    again = ctx.model_copy(update={"bank_ref_asks_since_expiry": 1})
    assert run(again, A.ASK_BANK_REF).rule_id == "R-PAYMENT-REPORT-EXPIRED"


def test_c_hours_queues(make_ctx):
    night = make_ctx(State.PAPERWORK, now=NOW.replace(hour=22))
    sunday = make_ctx(State.PAPERWORK, now=NOW + timedelta(days=2))
    for ctx in (night, sunday):
        verdict = run(ctx, A.REQUEST_ONE_ITEM)
        assert (verdict.rule_id, verdict.fallback) == ("C-HOURS", Fallback.QUEUE)


def test_c_hours_never_hides_a_refusal(make_ctx):
    night = make_ctx(State.PAPERWORK, now=NOW.replace(hour=22))
    assert run(night, A.ESCALATION).rule_id == "S-PAPERWORK-F1"
    assert run(night, A.REQUEST_ONE_ITEM, text="Pay or we take legal action").rule_id == "C-TONE"


def test_c_cap(make_ctx):
    assert run(make_ctx(State.PAPERWORK, sent_this_week=2), A.REQUEST_ONE_ITEM).rule_id == "C-CAP"


@pytest.mark.parametrize(
    "text", ["We will take legal action.", "Your CIBIL record", "Pay today!", "This is URGENT",
             "PAY THIS AMOUNT today", "This is your final warning."],
)
def test_tone_lint_catches(text):
    assert lint_tone(text)


def test_tone_lint_passes_polite_indian_business_text():
    assert lint_tone("As a courtesy, please share the UTR for your NEFT payment against INV-1041. GSTIN on file.") == []


def test_c_leak(make_ctx, policy):
    ctx = make_ctx(State.CASH_CONSTRAINT)
    leak = run(ctx, A.DECLINE_CONDITION, text="We can only offer up to 60 days with 25% upfront.")
    assert leak.rule_id == "C-LEAK"
    terms = "Invoice payment terms: Net 60 days."
    assert run(ctx, A.DECLINE_CONDITION, text="Per the Net 60 days terms on this invoice.", facts_text=terms).allowed
    assert lint_leak("Up to 3 equal instalments", "", policy.limits)
    assert lint_leak("within 160 days of 125%", "", policy.limits) == []
    assert run(ctx, A.DECLINE_CONDITION, text="We're not able to accept that condition on this invoice.").allowed


FIFTY_FIFTY = ((210000, None), (210000, None))


@pytest.mark.parametrize(
    "p,rule",
    [
        (plan(*FIFTY_FIFTY, concessions=["late_fee_waiver"]), "L-CONCESSION"),
        (plan(*FIFTY_FIFTY, concessions=["unknown_condition"]), "L-CONCESSION"),
        (plan(*FIFTY_FIFTY, concessions=["discount"]), "L-DISCOUNT"),
        (plan(*[(100, None)] * 4), "L-INSTALMENTS"),
        (plan((10, None), (90, None)), "L-UPFRONT"),
        (plan((50, date(2026, 12, 1)), (50, date(2027, 6, 1))), "L-TENURE"),
    ],
)
def test_client_limits(make_ctx, p, rule):
    assert run(make_ctx(State.CASH_CONSTRAINT), A.PROPOSE_PLAN, plan=p).rule_id == rule


def test_unresolved_dates_can_be_proposed_but_not_activated(make_ctx):
    ctx = make_ctx(State.CASH_CONSTRAINT)
    loose = plan(*FIFTY_FIFTY)
    assert run(ctx, A.PROPOSE_PLAN, plan=loose).allowed
    confirmed = loose.model_copy(update={"buyer_confirmed": True})
    assert run(ctx, A.ACTIVATE_PROMISE, plan=confirmed).rule_id == "L-TENURE"
    exact = plan((210000, date(2026, 12, 10)), (210000, date(2027, 1, 5)))
    exact = exact.model_copy(update={"buyer_confirmed": True})
    assert run(ctx, A.ACTIVATE_PROMISE, plan=exact).allowed
