from conftest import contact, evidence

from dlra.gates import g_contact, g_diag, g_pay, resolve_contacts
from dlra.models import Allocation, Diagnosis

LEDGER = "accounts@godavari-elec.example"
NEW = "godavari.accts@mailbox.example"


def statuses(contacts, **kw):
    return {(c.email, c.source): str(c.status) for c in resolve_contacts(contacts, **kw)}


def c(email, source="ledger"):
    return contact(email, status="unverified", source=source, id=f"{email}/{source}")


def test_ledger_alone_is_unverified_until_the_buyer_replies_from_it():
    assert statuses([c(LEDGER)]) == {(LEDGER, "ledger"): "unverified"}
    assert statuses([c(LEDGER)], replied_from={LEDGER.upper()}) == {(LEDGER, "ledger"): "verified"}


def test_signature_or_scrape_never_verifies_itself():
    assert statuses([c(NEW, "email")], replied_from={NEW}) == {(NEW, "email"): "unverified"}


def test_different_address_from_another_source_conflicts_both():
    got = statuses([c(LEDGER), c(NEW, "email")], replied_from={LEDGER})
    assert set(got.values()) == {"conflicting"}


def test_human_pick_resolves_the_conflict():
    got = statuses([c(LEDGER), c(NEW, "email")], human_confirmed={LEDGER})
    assert got == {(LEDGER, "ledger"): "verified", (NEW, "email"): "unverified"}


def test_corroboration_neither_verifies_nor_conflicts():
    assert set(statuses([c(LEDGER), c(LEDGER, "olostep")]).values()) == {"unverified"}


def test_demo_case_5_olostep_backs_the_ledger_but_the_conflict_stands():
    got = statuses([c(LEDGER), c(LEDGER, "olostep"), c(NEW, "email")], replied_from={LEDGER})
    assert got[(NEW, "email")] == "conflicting" and got[(LEDGER, "ledger")] == "conflicting"


def test_g_contact():
    ok = contact()
    assert g_contact(ok, [ok]).allowed
    assert not g_contact(None).allowed
    assert not g_contact(contact(opted_out=True)).allowed


def test_g_diag_threshold_and_conflicting_key_evidence():
    d = Diagnosis(state="PAPERWORK", confidence=0.85, evidence_ids=["E1"])
    assert g_diag(d, [evidence()], 0.7).allowed
    assert not g_diag(d.model_copy(update={"confidence": 0.69}), [evidence()], 0.7).allowed
    assert not g_diag(d, [evidence(status="conflicting")], 0.7).allowed
    assert g_diag(d, [evidence(), evidence("E9", status="conflicting")], 0.7).allowed


def test_g_pay_needs_confidence_reference_and_exact_amount():
    good = Allocation(lines={"I1": 100000}, kind="exact_gross", amount_paise=100000, confidence=0.98,
                      cites_invoice=True)
    assert g_pay(good, 100000).allowed
    assert not g_pay(good.model_copy(update={"confidence": 0.9}), 100000).allowed
    assert not g_pay(good.model_copy(update={"cites_invoice": False}), 100000).allowed
    assert not g_pay(good, 100050).allowed
    assert g_pay(good, 102000, tds_evidenced_paise=2000).allowed
    assert not g_pay(None, 100000).allowed
