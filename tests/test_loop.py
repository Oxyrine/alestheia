"""End to end through the database: events, loader, engine, outbox, deadlines, human decisions."""
import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dlra import api, events, loader, mailer, outbox, scheduler, serve, service
from dlra.db import connect
from dlra.models import Diagnosis, State
from dlra.policy import load_policy
from scripts import demo_world as world
from scripts.replay_demo import replay

NOW = world.NOW


class World:
    """The demo database after the replay, with an overridable diagnosis per case."""

    def __init__(self, path=":memory:"):
        self.conn = connect(path)
        self.states = dict(world.DIAGNOSES)
        self.deps = service.Deps(policy=load_policy(), propose=self.propose)
        world.seed(self.conn)
        self.sent = replay(self.conn, self.deps, say=lambda *_: None)

    def propose(self, bundle):
        base = world.scripted_diagnosis(bundle)
        return base.model_copy(update={"state": State(self.states[bundle.case_id])})

    def rows(self, sql, *args):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def card(self, case_id, kind):
        return self.one("SELECT id FROM cards WHERE case_id = ? AND kind = ? AND status = 'open'", case_id, kind)

    def decide(self, case_id, kind, decision="approve", now=NOW, **options):
        service.decide_card(self.conn, self.card(case_id, kind), decision, self.deps, now, options=options)
        return service.dispatch(self.conn, self.deps, now)

    def messages(self, case_id):
        return {r["intent"]: r["status"] for r in self.rows("SELECT * FROM messages WHERE case_id = ?", case_id)}

    def bundle(self, case_id, now=NOW):
        return loader.load_bundle(self.conn, case_id, now)

    def flush(self, now=NOW, send=None):
        return outbox.flush(self.conn, self.deps.policy, now, send or (lambda *m: self.sent.append(m)))


@pytest.fixture
def w():
    return World()


def test_replay_reaches_the_six_demo_outcomes(w):
    assert {r["id"]: r["state"] for r in w.rows("SELECT id, state FROM cases")} == world.DIAGNOSES
    cards = {(r["case_id"], r["kind"]) for r in w.rows("SELECT case_id, kind FROM cards")}
    assert {
        ("K-1041", "material_correction"), ("K-1052", "dispute_dossier"), ("K-882", "out_of_policy_request"),
        ("K-1060", "ambiguous_allocation"), ("K-1077", "low_conf_contact"), ("K-1089", "distress_review"),
    } <= cards
    assert w.messages("K-1052") == {"payment_reminder": "blocked", "dispute_ack_request_evidence": "sent"}
    # blocked because of the dispute, not for want of a diagnosis
    assert w.one("SELECT rule_id FROM messages WHERE intent = 'payment_reminder'") == "H-04"
    assert w.messages("K-882") == {"decline_condition_neutral": "sent", "confirmation_request_exact_dates": "sent"}
    assert w.messages("K-1060") == {"ask_bank_reference": "sent"}
    assert w.messages("K-1041") == w.messages("K-1077") == {}
    assert w.messages("K-1089") == {"neutral_question": "sent"}
    assert not any("godavari" in to for to, _, _ in w.sent)


def test_replay_invariants(w):
    assert w.one("SELECT COUNT(*) FROM allocations WHERE status = 'confirmed'") == 0
    assert w.one("SELECT COUNT(*) FROM decisions WHERE allowed = 0 AND card_id IS NULL") == 0
    assert w.bundle("K-882").promise.status == "awaiting_confirmation"


def test_loop_guard_the_agents_own_events_wake_nothing(w):
    runs = w.one("SELECT COUNT(*) FROM runs")
    assert w.one("SELECT COUNT(*) FROM events WHERE kind IN ('message_sent', 'run_completed')") > 0
    assert service.dispatch(w.conn, w.deps, NOW) == []
    assert w.one("SELECT COUNT(*) FROM runs") == runs


def test_duplicate_payment_is_stored_once(w):
    line = world.bank_line()
    key = events.bank_key(str(line.value_date), line.amount_paise, line.narration, line.id)
    assert service.record_payment(w.conn, line, key) is None
    assert w.one("SELECT COUNT(*) FROM payments") == 1


def test_a_rerun_repeats_no_message_and_no_card(w):
    before = w.rows("SELECT COUNT(*) AS c, (SELECT COUNT(*) FROM messages) AS m FROM cards")
    for case_id in world.DIAGNOSES:
        events.append(w.conn, "evidence_changed", "test", {}, f"again:{case_id}", case_id)
    service.dispatch(w.conn, w.deps, NOW)
    assert w.rows("SELECT COUNT(*) AS c, (SELECT COUNT(*) FROM messages) AS m FROM cards") == before


# --- send time -----------------------------------------------------------------


def queue(w, case_id="K-1041", contact="CT-KAV-ledger", action="REQUEST_ONE_ITEM"):
    w.conn.execute(
        "INSERT INTO messages (id, case_id, contact_id, action, intent, subject, body, status) "
        "VALUES ('Q1', ?, ?, ?, 'q', 'Subject', 'A polite note.', 'queued')", (case_id, contact, action))


def status(w, message_id="Q1"):
    return w.one("SELECT status || ':' || COALESCE(rule_id, '') FROM messages WHERE id = ?", message_id)


def test_flush_sends_an_allowed_message(w):
    queue(w)
    assert w.flush()["sent"] == 1 and status(w) == "sent:"


def test_flush_reauthorises_because_things_change_after_queueing(w):
    queue(w)
    service.insert_evidence(w.conn, world.ev("K-1041", 9, "buyer_page", "payment_report", "paid",
                                             {"amount_paise": 11800000, "paid_on": "2026-11-27"}, days_ago=0))
    assert w.flush()["blocked"] == 1 and status(w) == "blocked:H-04"
    assert w.card("K-1041", "contact_blocked")


def test_flush_checks_the_address_the_message_was_written_to(w):
    queue(w, "K-1077", "CT-GOD-email", "NEUTRAL_QUESTION_ONCE")
    w.flush()
    assert status(w) == "blocked:H-02"


def test_flush_leaves_it_queued_out_of_hours_and_on_send_failure(w):
    queue(w)
    assert w.flush(now=NOW.replace(hour=22))["queued"] == 1 and status(w) == "queued:"

    def broken(*_):
        raise OSError("smtp down")

    assert w.flush(send=broken)["failed"] == 1 and status(w) == "queued:"


# --- human decisions -------------------------------------------------------------


@pytest.mark.parametrize("accept_tds", [True, False])
def test_supplier_confirms_the_allocation_and_tds_is_a_separate_yes(w, accept_tds):
    w.decide("K-1060", "ambiguous_allocation", candidate=0, accept_tds=accept_tds)
    confirmed = w.rows("SELECT * FROM allocations WHERE status = 'confirmed' ORDER BY invoice_id")
    assert [r["invoice_id"] for r in confirmed] == ["INV-1060", "INV-1061", "INV-1062"]
    assert sum(r["amount_paise"] for r in confirmed) == 44486000
    assert sum(r["tds_reported_paise"] for r in confirmed) == 767000
    assert sum(r["tds_evidenced_paise"] for r in confirmed) == (767000 if accept_tds else 0)
    facts = w.bundle("K-1060").facts
    assert facts.allocated_payment_ids == ["PAY-1"]
    open_ids = {i.id for i in w.bundle("K-1060").open_invoices}
    assert open_ids == ({"INV-1063"} if accept_tds else {"INV-1060", "INV-1061", "INV-1062", "INV-1063"})


def test_a_card_is_decided_once(w):
    card = w.card("K-1041", "material_correction")
    service.decide_card(w.conn, card, "approve", w.deps, NOW)
    with pytest.raises(service.DecisionError):
        service.decide_card(w.conn, card, "reject", w.deps, NOW)
    service.dispatch(w.conn, w.deps, NOW)
    assert w.one("SELECT COUNT(*) FROM cards WHERE case_id = 'K-1041' AND kind = 'material_correction'") == 1


@pytest.mark.parametrize("options", [{"candidate": 7}, {"candidate": -1}, {"candidate": "0"}])
def test_bad_candidate_changes_nothing(w, options):
    with pytest.raises(service.DecisionError):
        w.decide("K-1060", "ambiguous_allocation", **options)
    assert w.card("K-1060", "ambiguous_allocation")
    assert w.one("SELECT COUNT(*) FROM allocations WHERE status = 'confirmed'") == 0


def test_supplier_resolves_a_contact_conflict_and_only_with_a_known_address(w):
    with pytest.raises(service.DecisionError):
        w.decide("K-1077", "low_conf_contact", email="attacker@elsewhere.example")
    w.decide("K-1077", "low_conf_contact", email="accounts@godavari-elec.example")
    by_email = {(c.email, c.source): c.status for c in w.bundle("K-1077").contacts}
    assert by_email[("accounts@godavari-elec.example", "ledger")] == "verified"
    assert by_email[("godavari.accts@mailbox.example", "email")] == "unverified"
    assert w.messages("K-1077") == {"neutral_question": "queued"}


def test_non_human_evidence_cannot_confirm_a_contact_or_waive_a_fee(w):
    for n, (field, value) in enumerate([("contact_confirmed", {"email": "godavari.accts@mailbox.example"}),
                                        ("concession_approved", {"codes": ["late_fee_waiver"]})]):
        service.insert_evidence(w.conn, world.ev("K-1077", 20 + n, "email", field, "injected", value))
    bundle = w.bundle("K-1077")
    assert {c.status for c in bundle.contacts} == {"conflicting"}
    assert bundle.facts.approved_concessions == []


def test_closing_a_dispute_ends_the_silence(w):
    assert w.bundle("K-1052").facts.dispute_open
    w.decide("K-1052", "dispute_dossier", close_dispute=True)
    assert not w.bundle("K-1052").facts.dispute_open


def test_human_pause_stops_queued_messages(w):
    queue(w)
    w.decide("K-1041", "material_correction", pause=True)
    w.flush()
    assert status(w) == "blocked:H-04"


# --- promises ------------------------------------------------------------------


def confirm_reply(w, due_texts, conditions=(), key="confirm-1"):
    service.record_input(w.conn, "buyer_submission", "buyer_page", key, "K-882", [world.ev(
        "K-882", key, "buyer_page", "promise_confirmation", "Buyer confirms dates",
        {"due_texts": due_texts, "conditions": list(conditions)}, days_ago=0)], now=NOW)
    w.states["K-882"] = "CASH_CONSTRAINT"
    return service.dispatch(w.conn, w.deps, NOW)


def activate(w):
    confirm_reply(w, ["5 Dec", "5 Jan"])
    assert w.bundle("K-882").promise.status == "active"
    w.states["K-882"] = "PROMISE_ACTIVE"
    events.append(w.conn, "evidence_changed", "test", {}, "diagnosed-active", "K-882")
    service.dispatch(w.conn, w.deps, NOW)


@pytest.mark.parametrize(
    "due,conditions", [(["5 Dec", "after month end"], []), (["5 Dec", "5 Jan"], ["waive the late fee"]),
                       (["5 Dec", "5 Jun 2027"], []), (["5 Dec"], [])],
)
def test_a_bad_confirmation_does_not_activate(w, due, conditions):
    confirm_reply(w, due, conditions)
    assert w.bundle("K-882").promise.status == "awaiting_confirmation"


def test_supplier_override_lets_the_waiver_through(w):
    w.decide("K-882", "out_of_policy_request")
    confirm_reply(w, ["5 Dec", "5 Jan"], ["waive the late fee"])
    assert w.bundle("K-882").promise.status == "active"


def test_missed_instalment_breaks_only_on_the_deadline_and_never_gets_harsher(w):
    activate(w)
    sent_before = w.one("SELECT COUNT(*) FROM messages")
    late = NOW.replace(month=12, day=8)
    w.states["K-882"] = "PROMISE_BROKEN"
    assert scheduler.scan_deadlines(w.conn, w.deps.policy, late) > 0
    service.dispatch(w.conn, w.deps, late)
    assert w.bundle("K-882", late).promise.status == "broken"
    assert w.one("SELECT state FROM cases WHERE id = 'K-882'") == "PROMISE_BROKEN"
    assert w.card("K-882", "promise_broken")
    assert w.one("SELECT COUNT(*) FROM messages") == sent_before
    assert w.rows("SELECT rule_id FROM decisions WHERE case_id = 'K-882' AND action = 'HARSHER_MESSAGE'")


def test_a_paid_instalment_keeps_the_promise_active(w):
    activate(w)
    w.conn.execute("INSERT INTO allocations (id, payment_id, invoice_id, candidate, amount_paise, status) "
                   "VALUES ('A1', 'P-NEW', 'INV-882', 0, 21000000, 'confirmed')")
    late = NOW.replace(month=12, day=8)
    scheduler.scan_deadlines(w.conn, w.deps.policy, late)
    service.dispatch(w.conn, w.deps, late)
    assert w.bundle("K-882", late).promise.status == "active"


def test_scanning_twice_adds_nothing(w):
    later = NOW + timedelta(days=30)
    assert scheduler.scan_deadlines(w.conn, w.deps.policy, later) > 0
    assert scheduler.scan_deadlines(w.conn, w.deps.policy, later) == 0


# --- the reported payment that never arrives -------------------------------------


def report(w, key, now, utr):
    item = world.ev("K-1041", key, "buyer_page", "payment_report", "We have paid",
                    {"amount_paise": 11800000, "paid_on": str(now.date()), "utr": utr}, days_ago=0,
                    status="unverified").model_copy(update={"ts": now})
    service.record_input(w.conn, "buyer_submission", "buyer_page", key, "K-1041", [item], now=now)
    return item.id


def test_unsupported_payment_report_expires_into_a_card_and_one_ask(w):
    w.states["K-1041"] = "PAID_UNMATCHED"
    first = report(w, "r1", NOW, "UTR000000000001")
    service.dispatch(w.conn, w.deps, NOW)
    assert w.bundle("K-1041").facts.payment_reported
    w.flush()

    later = NOW + timedelta(days=6)  # a Thursday
    w.states["K-1041"] = "SILENT_UNKNOWN"
    scheduler.tick(w.conn, w.deps, later)
    assert w.one("SELECT status FROM evidence WHERE id = ?", first) == "expired_unverified"
    facts = w.bundle("K-1041", later).facts
    assert facts.payment_report_expired and not facts.payment_reported
    assert w.card("K-1041", "reported_payment_not_found")
    asks = w.rows("SELECT intent, action FROM messages WHERE case_id = 'K-1041'")
    assert [a["intent"] for a in asks if a["intent"] == "ask_bank_reference_after_expiry"] \
        == ["ask_bank_reference_after_expiry"]
    assert not [a for a in asks if a["action"] in ("PAYMENT_REMINDER", "ESCALATION", "NEUTRAL_QUESTION_ONCE")]

    report(w, "r2", later, "UTR000000000002")  # a second claim earns no new silence
    facts = w.bundle("K-1041", later).facts
    assert facts.payment_report_expired and not facts.payment_reported

    w.decide("K-1041", "reported_payment_not_found", now=later)
    assert not w.bundle("K-1041", later).facts.payment_report_expired


def test_a_report_matched_by_the_bank_does_not_expire(w):
    first = world.inputs()[3][4][0].id
    later = NOW + timedelta(days=8)
    scheduler.tick(w.conn, w.deps, later)
    assert w.one("SELECT status FROM evidence WHERE id = ?", first) == "unverified"


# --- boundaries --------------------------------------------------------------------


def test_scheduler_never_imports_the_mailer():
    tree = ast.parse(Path(scheduler.__file__).read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names]
    modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any("mailer" in x or "smtplib" in x for x in names + modules)


@pytest.mark.parametrize("to", ["real@gmail.com", "a@x.example, b@y.com", "Name <a@x.example>", "nobody",
                                "a@x.example\nBcc: b@y.com"])
def test_mailer_refuses(to):
    with pytest.raises(ValueError):
        mailer.send(to, "s", "b", env={})


def test_mailer_log_mode_and_header_injection():
    mailer.send("a@x.example", "s", "b", env={})
    with pytest.raises(ValueError):
        mailer.send("a@x.example", "s\nBcc: b@y.com", "b", env={})


@pytest.fixture
def client(tmp_path):
    w = World(tmp_path / "demo.db")
    app = api.create_app(str(tmp_path / "demo.db"), w.deps, token="t0ken", clock=lambda: NOW)
    return w, TestClient(app)


AUTH = {"Authorization": "Bearer t0ken"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer "}])
def test_api_needs_the_token(client, headers):
    _, http = client
    for path in ("/api/cards", "/api/cases", "/api/rules"):
        assert http.get(path, headers=headers).status_code == 401
    assert http.post("/api/cards/x/decision", json={"decision": "approve"}, headers=headers).status_code == 401


def test_api_with_no_token_configured_serves_nothing(tmp_path):
    deps = service.Deps(load_policy(), lambda b: Diagnosis(state="DISPUTE", confidence=0))
    http = TestClient(api.create_app(str(tmp_path / "x.db"), deps, token=""))
    assert http.get("/api/cards", headers={"Authorization": "Bearer "}).status_code == 401


def test_api_reads_and_decides(client):
    w, http = client
    cards = http.get("/api/cards", headers=AUTH).json()
    target = next(c for c in cards if c["kind"] == "ambiguous_allocation")
    assert len(target["payload"]["candidates"]) == 2
    assert http.get(f"/api/cards/{target['id']}", headers=AUTH).json()["evidence"]

    case = http.get("/api/cases/K-1060", headers=AUTH).json()
    assert case["settlement"]["cash_received_paise"] == 0 and case["diagnosis"]["state"] == "PAID_UNMATCHED"
    assert http.get(f"/api/decisions/{case['decisions'][0]['id']}", headers=AUTH).status_code == 200
    assert len(http.get("/api/cases", headers=AUTH).json()) == 6
    assert len(http.get("/api/cases?state=DISPUTE", headers=AUTH).json()) == 1
    assert "DISPUTE" in http.get("/api/rules", headers=AUTH).json()["states"]

    url = f"/api/cards/{target['id']}/decision"
    assert http.post(url, headers=AUTH, json={"decision": "delete"}).status_code == 422
    assert http.post(url, headers=AUTH, json={"decision": "approve", "state": "RESOLVED"}).status_code == 422
    done = http.post(url, headers=AUTH, json={"decision": "approve", "options": {"candidate": 0}})
    assert done.status_code == 200 and done.json()["reran"] == ["K-1060"]
    assert http.post(url, headers=AUTH, json={"decision": "approve"}).status_code == 409
    assert http.get("/api/cards/nope", headers=AUTH).status_code == 404
    assert http.get("/api/cases/nope", headers=AUTH).status_code == 404
    assert json.loads(done.text)["card_id"] == target["id"]
    assert w.one("SELECT COUNT(*) FROM allocations WHERE status = 'confirmed'") == 3


def part_paid(w, utr="UTR-OLD-0000001"):
    """An earlier, confirmed part-payment on INV-1041."""
    w.conn.execute("INSERT INTO payments (id, utr, amount_paise, value_date, dedup_key) "
                   "VALUES ('P-PART', ?, 5000000, '2026-10-15', 'k-part')", (utr,))
    w.conn.execute("INSERT INTO allocations (id, payment_id, invoice_id, candidate, amount_paise, status) "
                   "VALUES ('A-PART', 'P-PART', 'INV-1041', 0, 5000000, 'confirmed')")


def test_a_new_report_after_a_part_payment_still_silences_and_still_expires(w):
    part_paid(w)
    w.states["K-1041"] = "PAID_UNMATCHED"
    second = report(w, "r1", NOW, "UTR000000000009")
    assert w.bundle("K-1041").facts.payment_reported
    later = NOW + timedelta(days=6)
    w.states["K-1041"] = "SILENT_UNKNOWN"
    scheduler.tick(w.conn, w.deps, later)
    assert w.one("SELECT status FROM evidence WHERE id = ?", second) == "expired_unverified"


def test_a_report_already_reconciled_is_no_longer_live(w):
    part_paid(w, utr="UTR000000000001")
    first = report(w, "r1", NOW, "UTR000000000001")
    assert not w.bundle("K-1041").facts.payment_reported
    scheduler.tick(w.conn, w.deps, NOW + timedelta(days=6))
    assert w.one("SELECT status FROM evidence WHERE id = ?", first) == "unverified"


def test_an_open_card_follows_the_latest_candidates_and_can_still_be_approved(w):
    w.conn.execute("UPDATE invoices SET customer_id = 'KAV' WHERE id IN ('INV-1061', 'INV-1062')")
    events.append(w.conn, "evidence_changed", "test", {}, "ledger-fixed", "K-1060")
    service.dispatch(w.conn, w.deps, NOW)
    payload = json.loads(w.one("SELECT payload FROM cards WHERE case_id = 'K-1060' AND kind = 'ambiguous_allocation'"))
    assert [sorted(c["lines"]) for c in payload["candidates"]] == [["INV-1060", "INV-1063"]]
    w.decide("K-1060", "ambiguous_allocation", candidate=0)
    assert w.one("SELECT COUNT(*) FROM allocations WHERE status = 'confirmed'") == 2


def test_a_case_left_locked_by_a_killed_process_is_released_at_startup(tmp_path):
    path = str(tmp_path / "s.db")
    w = World(path)
    w.conn.execute("UPDATE cases SET running = 1")
    scheduler.release_stale_runs(path)
    assert w.one("SELECT SUM(running) FROM cases") == 0


def test_the_scheduler_registers_single_instance_jobs(tmp_path):
    jobs = scheduler.start(str(tmp_path / "s.db"), World().deps, lambda *_: None)
    try:
        assert {j.id for j in jobs.get_jobs()} == {"deadlines", "outbox"}
        assert all(j.max_instances == 1 and j.coalesce for j in jobs.get_jobs())
    finally:
        jobs.shutdown(wait=False)


def test_the_deployed_app_boots_with_the_demo_and_stays_locked(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_TOKEN", "t0ken")
    with TestClient(serve.create_app(str(tmp_path / "boot.db"))) as http:
        assert http.get("/healthz").json()["ok"]
        assert http.get("/api/cases").status_code == 401
        assert len(http.get("/api/cases", headers=AUTH).json()) == 6


def test_boot_refuses_to_delete_anything_but_a_database(tmp_path):
    keep = tmp_path / "notes.txt"
    keep.write_text("keep me")
    with pytest.raises(ValueError):
        serve.create_app(str(keep))
    assert keep.exists()
