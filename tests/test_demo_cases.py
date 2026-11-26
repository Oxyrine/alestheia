"""The six demo outcomes, engine side.

ponytail: bundles are hand-built and the diagnosis is given. When data/demo (lane C) and
diagnose.py (lane B) land, build these from them; the assertions stay.
"""
from datetime import date

import pytest
from conftest import NOW, contact, evidence

from dlra.engine import decide, describe
from dlra.models import Action as A
from dlra.models import CaseBundle, CaseFacts, Customer, Diagnosis, Invoice, Payment, State

R = 100


def inv(number, taxable, gross=None, due=date(2026, 11, 1)):
    gross = gross or taxable * 118 // 100
    return Invoice(id=number, number=number, customer_id="C1", issue_date=date(2026, 10, 1), due_date=due,
                   taxable_paise=taxable * R, gst_paise=(gross - taxable) * R, gross_paise=gross * R)


def bundle(invoice, state_evidence=("E1",), contacts=None, facts=None, customer=None, **kw):
    return CaseBundle(
        case_id=f"K-{invoice.number}", invoice=invoice,
        customer=customer or Customer(id="C1", canonical_name="Buyer"),
        contacts=[contact()] if contacts is None else contacts,
        evidence=[evidence(i) for i in state_evidence], facts=CaseFacts(**(facts or {})), **kw,
    )


def run(policy, case, state, confidence=0.85):
    return decide(case, Diagnosis(state=state, confidence=confidence, evidence_ids=["E1"]), policy, NOW)


def refused(result, action):
    return [d.verdict.rule_id for d in result.refusals() if d.action == action]


def assert_every_refusal_has_a_card(result):
    kinds = {c.kind for c in result.cards}
    for decision in result.refusals():
        assert decision.card_kind in kinds, describe(result)


def test_case_1_missing_po(policy):
    got = run(policy, bundle(inv("INV-1041", 100000), facts={"correction_on_file": True}), State.PAPERWORK)
    assert [c.kind for c in got.cards] == ["material_correction"]
    assert refused(got, A.ESCALATION) == ["S-PAPERWORK-F1"]
    assert got.messages == []
    assert_every_refusal_has_a_card(got)


def test_case_2_damaged_goods_dispute(policy):
    got = run(policy, bundle(inv("INV-1052", 73220, 86400), facts={"dispute_open": True}), State.DISPUTE)
    assert got.card("dispute_dossier")
    assert [m.intent for m in got.messages] == ["dispute_ack_request_evidence"]
    assert not got.block_queued.allowed and got.block_queued.rule_id == "H-04"
    assert refused(got, A.PAYMENT_REMINDER) == ["H-04"]
    assert_every_refusal_has_a_card(got)


def test_case_3_fee_waiver_promise(policy):
    extract = {
        "invoice_ids": ["INV-882"],
        "installments": [{"share": 0.5, "due_text": "next week"}, {"due_text": "after month end"}],
        "conditions": ["waive late fee"],
    }
    case = bundle(inv("INV-882", 355932, 420000), facts={"promise_extract": extract})
    got = run(policy, case, State.CASH_CONSTRAINT)
    assert refused(got, A.PROPOSE_PLAN) == ["L-CONCESSION"]
    assert [m.intent for m in got.messages] == ["decline_condition_neutral", "confirmation_request_exact_dates"]
    assert got.card("out_of_policy_request")
    assert got.promise.status == "awaiting_confirmation"
    assert [i.amount_paise for i in got.promise.installments] == [210000 * R] * 2
    assert {i.date_status for i in got.promise.installments} == {"unresolved"}
    assert_every_refusal_has_a_card(got)


SAHYADRI = [inv("INV-1060", 158500), inv("INV-1061", 125000), inv("INV-1062", 100000), inv("INV-1063", 218500)]


def sahyadri_case():
    payment = Payment(id="P1", amount_paise=444860 * R, value_date=date(2026, 11, 20), narration="NEFT-XYZ",
                      payer_name="SAHYADRI RETAIL")
    customer = Customer(id="C1", canonical_name="Sahyadri Retail Pvt Ltd", tds_rate_bp=200,
                        tds_basis="contract_work", payment_history={"tds_deducted_ratio": 1.0, "n_payments": 6})
    return bundle(SAHYADRI[0], customer=customer, open_invoices=SAHYADRI, payments=[payment],
                  facts={"payment_reported": True, "reported_tds_paise": 7670 * R})


def test_case_4_unreferenced_multi_invoice_tds(policy):
    got = run(policy, sahyadri_case(), State.PAID_UNMATCHED)
    assert [set(c.lines) for c in got.match.candidates] == [
        {"INV-1060", "INV-1061", "INV-1062"}, {"INV-1060", "INV-1063"}]
    assert got.match.outcome == "favoured" and got.confirmed is None
    card = got.card("ambiguous_allocation")
    assert card.reason == "Likely, but not proven: no invoice reference (G-PAY) and TDS not evidenced (H-03)."
    assert [m.intent for m in got.messages] == ["ask_bank_reference"]
    assert refused(got, A.MARK_PAID) == ["H-03"]
    assert refused(got, A.PAYMENT_REMINDER) == ["H-04"]
    assert_every_refusal_has_a_card(got)


def test_case_5_conflicting_contact(policy):
    contacts = [
        contact("accounts@godavari-elec.example", status="conflicting", source="ledger"),
        contact("godavari.accts@mailbox.example", status="conflicting", source="email"),
    ]
    got = run(policy, bundle(inv("INV-1077", 54237, 64000), contacts=contacts), State.SILENT_UNKNOWN)
    assert got.messages == []
    assert refused(got, A.NEUTRAL_QUESTION_ONCE) == ["H-02"]
    card = got.card("low_conf_contact")
    assert card.payload["sub_tag"] == "possible_redirection_fraud" and "phone" in card.reason
    assert got.safe_pause_until
    assert_every_refusal_has_a_card(got)


@pytest.mark.parametrize("confidence", [0.85, 0.6])
def test_case_6_distress_with_good_history(policy, confidence):
    customer = Customer(id="C1", canonical_name="Konkan Marine Supplies",
                        payment_history={"on_time_ratio": 0.92, "n_payments": 36})
    case = bundle(inv("INV-1089", 203390, 240000), customer=customer, facts={"distress_signal": True})
    got = run(policy, case, State.SILENT_UNKNOWN, confidence)
    assert got.under_review and got.card("distress_review").rule_id == "R-DISTRESS-REVIEW"
    assert [m.intent for m in got.messages] == ["neutral_question"]
    assert refused(got, A.ESCALATION)
    assert_every_refusal_has_a_card(got)
