from datetime import date

import pytest
from conftest import NOW
from test_demo_cases import bundle, inv, run, sahyadri_case

from dlra.db import connect
from dlra.engine import decide, run_case
from dlra.models import Action as A
from dlra.models import Diagnosis, Payment, State


def test_expired_payment_report_one_ask_never_a_reminder(policy):
    case = bundle(inv("INV-1", 100000), facts={"payment_report_expired": True})
    got = run(policy, case, State.SILENT_UNKNOWN)
    assert got.card("reported_payment_not_found")
    assert [m.action for m in got.messages] == [A.ASK_BANK_REF]


def test_after_the_one_ask_nothing_more_goes_out(policy):
    case = bundle(inv("INV-1", 100000),
                  facts={"payment_report_expired": True, "bank_ref_asks_since_expiry": 1})
    assert run(policy, case, State.SILENT_UNKNOWN).messages == []


def test_referenced_exact_payment_resolves_the_case(policy):
    payment = Payment(id="P1", amount_paise=118000_00, value_date=date(2026, 11, 20), narration="NEFT INV-1",
                      payer_name="Buyer")
    case = bundle(inv("INV-1", 100000), payments=[payment])
    got = run(policy, case, State.PAID_UNMATCHED)
    assert got.confirmed and got.state == State.RESOLVED and got.messages == []


def test_llm_only_diagnosis_goes_to_a_human_and_changes_nothing(policy):
    case = bundle(inv("INV-1", 100000), facts={"correction_on_file": True})
    got = run(policy, case, State.PAPERWORK, confidence=0.6)
    assert got.under_review and got.card("low_conf_diagnosis")
    assert not any(d.verdict.allowed and d.action == A.DRAFT_CORRECTION for d in got.decisions)


def test_illegal_transition_keeps_the_state_and_raises_a_card(policy):
    case = bundle(inv("INV-1", 100000), state=State.RESOLVED)
    got = run(policy, case, State.DISPUTE)
    assert got.state == State.RESOLVED and got.cards[0].rule_id == "T-RESOLVED-TERMINAL"
    assert got.messages == [] and got.decisions == []


def test_rerun_does_not_repeat_a_message(policy):
    case = bundle(inv("INV-1", 100000), facts={"sent_intents": ["request_missing_item"]})
    assert run(policy, case, State.PAPERWORK).messages == []


def test_outside_business_hours_queues(policy):
    got = decide(bundle(inv("INV-1", 100000)), Diagnosis(state=State.PAPERWORK, confidence=0.85),
                 policy, NOW.replace(hour=23))
    assert [m.status for m in got.messages] == ["queued"]


def test_a_draft_that_breaks_tone_is_not_queued(policy):
    got = decide(bundle(inv("INV-1", 100000)), Diagnosis(state=State.PAPERWORK, confidence=0.85),
                 policy, NOW, draft=lambda *_: ("Final warning", "Pay or face legal action", ""))
    assert got.messages == [] and got.refusals()[0].verdict.rule_id == "C-TONE"


# --- persistence and single-flight -------------------------------------------


def seeded():
    conn = connect()
    conn.execute("INSERT INTO customers (id, canonical_name) VALUES ('C1', 'Sahyadri')")
    conn.execute("INSERT INTO invoices (id, number, customer_id, taxable_paise, gst_paise, gross_paise) "
                 "VALUES ('INV-1060', 'INV-1060', 'C1', 1, 0, 1)")
    conn.execute("INSERT INTO cases (id, invoice_id) VALUES ('K-INV-1060', 'INV-1060')")
    return conn


def go(conn, policy, load=lambda *_: sahyadri_case(), propose=None):
    propose = propose or (lambda _: Diagnosis(state=State.PAID_UNMATCHED, confidence=0.85, evidence_ids=["E1"]))
    return run_case(conn, "K-INV-1060", "EVT-1", load, propose, policy, NOW)


def count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def test_run_is_persisted_with_cards_linked_to_refusals_and_nothing_confirmed(policy):
    conn = seeded()
    go(conn, policy)
    assert count(conn, "SELECT COUNT(*) FROM decisions WHERE allowed = 0 AND card_id IS NULL") == 0
    assert count(conn, "SELECT COUNT(*) FROM allocations WHERE status = 'confirmed'") == 0
    assert count(conn, "SELECT COUNT(DISTINCT candidate) FROM allocations") == 2
    assert count(conn, "SELECT COUNT(*) FROM events WHERE kind = 'run_completed' AND triggers_run = 0") == 1
    assert count(conn, "SELECT running FROM cases") == 0


def test_second_run_does_not_duplicate_open_cards(policy):
    conn = seeded()
    go(conn, policy)
    cards = count(conn, "SELECT COUNT(*) FROM cards")
    go(conn, policy)
    assert count(conn, "SELECT COUNT(*) FROM cards") == cards


def test_events_arriving_mid_run_coalesce_into_one_more_run(policy):
    conn = seeded()
    calls = []

    def load(c, _):
        if not calls:
            for _ in range(3):  # three events land while the first run is in flight
                assert go(c, policy) == []
        calls.append(1)
        return sahyadri_case()

    assert len(go(conn, policy, load=load)) == 2
    assert count(conn, "SELECT running + rerun_pending FROM cases") == 0


def test_a_crash_releases_the_case(policy):
    conn = seeded()
    with pytest.raises(RuntimeError):
        go(conn, policy, propose=lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    assert count(conn, "SELECT running FROM cases") == 0


def test_dispute_blocks_an_already_queued_reminder(policy):
    conn = seeded()
    conn.execute("INSERT INTO messages (id, case_id, action, status) "
                 "VALUES ('M1', 'K-INV-1060', 'PAYMENT_REMINDER', 'queued')")
    go(conn, policy, load=lambda *_: bundle(inv("INV-1060", 1000), facts={"dispute_open": True}),
       propose=lambda _: Diagnosis(state=State.DISPUTE, confidence=0.85))
    row = conn.execute("SELECT status, rule_id FROM messages WHERE id = 'M1'").fetchone()
    assert tuple(row) == ("blocked", "H-04")


def test_one_run_never_asks_for_the_bank_reference_twice_after_expiry(policy):
    case = bundle(inv("INV-1", 100000), facts={"payment_report_expired": True})
    got = run(policy, case, State.PAID_UNMATCHED)
    assert [m.action for m in got.messages] == [A.ASK_BANK_REF]


def test_a_cap_refusal_still_has_a_card(policy):
    got = run(policy, bundle(inv("INV-1", 100000), facts={"sent_this_week": 2}), State.PAPERWORK)
    refusal = got.refusals()[0]
    assert (refusal.verdict.rule_id, refusal.card_kind) == ("C-CAP", "contact_blocked")
    assert got.card("contact_blocked")


def test_rerun_replaces_proposed_allocations(policy):
    conn = seeded()
    go(conn, policy)

    def fewer(*_):
        case = sahyadri_case()
        return case.model_copy(update={"open_invoices": [case.open_invoices[0], case.open_invoices[3]]})

    go(conn, policy, load=fewer)
    assert count(conn, "SELECT COUNT(DISTINCT candidate) FROM allocations") == 1
    assert count(conn, "SELECT COUNT(*) FROM allocations") == 2
