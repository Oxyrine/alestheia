"""Everything that changes a case from outside a run: ingestion, deadlines, human decisions, dispatch."""
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

from dlra import engine, events, loader, promises, reconcile
from dlra.db import new_id, transaction, utc_iso
from dlra.models import Action as A
from dlra.models import (
    CaseBundle,
    Diagnosis,
    Evidence,
    EvidenceStatus,
    Payment,
    Promise,
    PromiseStatus,
    State,
)
from dlra.policy import Policy, ProposedAction, authorise


@dataclass
class Deps:
    policy: Policy
    propose: Callable[[CaseBundle], Diagnosis]  # lane B's diagnose.propose
    draft: engine.Drafter = engine.stub_draft


def _today(now: datetime, policy: Policy):
    return now.astimezone(ZoneInfo(policy.business_hours.tz)).date()


# --- ingestion ----------------------------------------------------------------


def insert_evidence(conn: sqlite3.Connection, item: Evidence) -> None:
    conn.execute(
        "INSERT INTO evidence (id, case_id, source_type, source_ref, ts, field, claim, value, confidence, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item.id, item.case_id, item.source_type, item.source_ref, utc_iso(item.ts), item.field, item.claim,
         json.dumps(item.value), item.confidence, item.status),
    )


def record_input(
    conn: sqlite3.Connection, kind: str, source: str, dedup_key: str, case_id: str | None,
    evidence: list[Evidence] = (), payload: dict | None = None, now: datetime | None = None,
) -> sqlite3.Row | None:
    """One inbound item and its evidence, atomically. A duplicate stores nothing."""
    with transaction(conn):
        event = events.append(conn, kind, source, payload or {}, dedup_key, case_id, ts=now)
        if event:
            for item in evidence:
                insert_evidence(conn, item)
    return event


def record_payment(
    conn: sqlite3.Connection, payment: Payment, dedup_key: str, kind: str = "bank_line_added",
    now: datetime | None = None,
) -> sqlite3.Row | None:
    with transaction(conn):
        event = events.append(conn, kind, payment.source, {"payment_id": payment.id}, dedup_key, ts=now)
        if event:
            conn.execute(
                "INSERT INTO payments (id, utr, amount_paise, value_date, narration, payer_name, source, dedup_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (payment.id, payment.utr, payment.amount_paise, payment.value_date.isoformat(), payment.narration,
                 payment.payer_name, payment.source, dedup_key),
            )
    return event


# --- effects applied before a run ---------------------------------------------


def _save_promise(conn, case_id: str, promise: Promise, baseline: int | None = None) -> None:
    conn.execute(
        "UPDATE promises SET data = ?, status = ?, baseline_paise = COALESCE(?, baseline_paise) "
        "WHERE id = ? AND case_id = ?",
        (promise.model_dump_json(), promise.status, baseline, promise.id, case_id),
    )


def _raise_card(conn, case_id: str, kind: str, reason: str, rule_id: str | None) -> None:
    engine._open_card(conn, case_id, engine.Card(kind, reason, rule_id))


def apply_confirmation(conn: sqlite3.Connection, case_id: str, deps: Deps, now: datetime) -> None:
    """A buyer's confirming reply activates a promise only if the dates are exact and policy accepts."""
    bundle = loader.load_bundle(conn, case_id, now)
    promise = bundle.promise
    replies = [e for e in bundle.evidence if e.field == "promise_confirmation" and e.status in loader.LIVE]
    if not replies or not promise or promise.status != PromiseStatus.AWAITING_CONFIRMATION:
        return
    case = conn.execute("SELECT state, diagnosis FROM cases WHERE id = ?", (case_id,)).fetchone()
    reply = replies[-1].value
    try:
        confirmed, violations = promises.confirm(
            promise, reply["due_texts"], deps.policy.limits, _today(now, deps.policy),
            conditions=reply.get("conditions") or [],
            approved_concessions=set(bundle.facts.approved_concessions),
        )
    except (ValueError, KeyError, TypeError) as error:
        _raise_card(conn, case_id, "out_of_policy_request", f"The confirmation could not be applied: {error}.", None)
        return
    if violations:
        _save_promise(conn, case_id, confirmed)
        _raise_card(conn, case_id, "out_of_policy_request", " ".join(r for _, r in violations), violations[0][0])
        return
    approved = set(bundle.facts.approved_concessions)
    checked = confirmed.model_copy(update={"concessions": sorted(set(confirmed.concessions) - approved)})
    context = engine.context_for(
        bundle, Diagnosis.model_validate_json(case["diagnosis"]), State(case["state"]), deps.policy, now
    )
    verdict = authorise(context, ProposedAction(action=A.ACTIVATE_PROMISE, plan=checked))
    if not verdict.allowed:
        _raise_card(conn, case_id, "low_conf_diagnosis", verdict.reason, verdict.rule_id)
        return
    _save_promise(conn, case_id, confirmed, bundle.facts.confirmed_paid.get(bundle.invoice.id, 0))


def apply_promise_progress(
    conn: sqlite3.Connection, case_id: str, deps: Deps, now: datetime, deadline: bool
) -> None:
    """Reconcile payments against an active promise. It can only break on a deadline."""
    promise, baseline = loader.load_promise(conn, case_id)
    if not promise or promise.status != PromiseStatus.ACTIVE:
        return
    bundle = loader.load_bundle(conn, case_id, now)
    paid = bundle.facts.confirmed_paid.get(bundle.invoice.id, 0) - baseline
    updated = promises.on_deadline(promise, paid, _today(now, deps.policy), deps.policy.promise_grace_days)
    # breaking needs a deadline, and money waiting to be matched is not a failed reconciliation
    if updated.status == PromiseStatus.BROKEN and (not deadline or bundle.payments):
        return
    _save_promise(conn, case_id, updated)


def expire_payment_report(conn: sqlite3.Connection, case_id: str, evidence_id: str, deps: Deps, now: datetime) -> None:
    bundle = loader.load_bundle(conn, case_id, now)
    report = next((e for e in bundle.evidence if e.id == evidence_id and e.status in loader.LIVE), None)
    seen = bundle.payments + loader.reconciled_payments(conn, bundle.invoice.id)
    if report is None or loader.payment_matches_report(
        report.value or {}, seen, deps.policy.tds.tolerance_paise_per_invoice
    ):
        return
    conn.execute(
        "UPDATE evidence SET status = ? WHERE id = ?", (EvidenceStatus.EXPIRED_UNVERIFIED, evidence_id)
    )


# --- dispatch -----------------------------------------------------------------


def _cases_for(conn: sqlite3.Connection, event: sqlite3.Row, deps: Deps, now: datetime) -> list[str]:
    if event["case_id"]:
        return [event["case_id"]]
    payment_id = json.loads(event["payload"]).get("payment_id")
    if not payment_id:
        return []
    out = []
    for row in conn.execute(
        "SELECT id FROM cases WHERE state IS NULL OR state != 'RESOLVED' ORDER BY id"
    ).fetchall():
        bundle = loader.load_bundle(conn, row["id"], now)
        if any(p.id == payment_id for p in bundle.payments):
            out.append(row["id"])
    return out


def dispatch(conn: sqlite3.Connection, deps: Deps, now: datetime) -> list[tuple[str, engine.RunResult]]:
    """Wake the engine for every triggering event not handled yet, oldest first."""
    pending = conn.execute(
        "SELECT * FROM events WHERE triggers_run = 1 AND id NOT IN (SELECT event_id FROM processed_events) "
        "ORDER BY rowid"
    ).fetchall()
    results = []
    for event in pending:
        payload = json.loads(event["payload"])
        deadline = payload.get("deadline") if event["kind"] == "deadline_reached" else None
        for case_id in _cases_for(conn, event, deps, now):
            if deadline == "payment_report":
                expire_payment_report(conn, case_id, payload["evidence_id"], deps, now)
            if deadline == "dispute_response":
                _raise_card(conn, case_id, "dispute_dossier", "The dispute response deadline has passed.", None)
            apply_confirmation(conn, case_id, deps, now)
            apply_promise_progress(conn, case_id, deps, now, deadline == "promise")
            for result in engine.run_case(
                conn, case_id, event["id"], partial(loader.load_bundle, now=now), deps.propose, deps.policy,
                now, deps.draft,
            ):
                results.append((case_id, result))
        # marked last: a crash re-runs the event, and runs are safe to repeat
        conn.execute("INSERT OR IGNORE INTO processed_events (event_id) VALUES (?)", (event["id"],))
    return results


# --- human decisions ----------------------------------------------------------

DECISIONS = {"approve": "approved", "reject": "rejected", "changes": "changes_requested"}


class DecisionError(ValueError):
    pass


def _confirm_allocation(conn, card, options: dict, deps: Deps, now: datetime) -> None:
    payload = json.loads(card["payload"])
    candidates, payment_id = payload.get("candidates") or [], payload.get("payment_id")
    index = options.get("candidate", 0)
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        raise DecisionError("there is no such candidate to confirm")
    if conn.execute(
        "SELECT 1 FROM allocations WHERE payment_id = ? AND status = 'confirmed'", (payment_id,)
    ).fetchone():
        raise DecisionError("this payment is already allocated")
    rows = conn.execute(
        "SELECT * FROM allocations WHERE payment_id = ? AND candidate = ? AND status = 'proposed'",
        (payment_id, index),
    ).fetchall()
    chosen = candidates[index]
    if {r["invoice_id"]: r["amount_paise"] for r in rows} != chosen["lines"]:
        raise DecisionError("the suggestions changed since this card was raised: review it again")

    cash, tds = loader.confirmed_totals(conn)
    for row in rows:
        invoice = conn.execute("SELECT * FROM invoices WHERE id = ?", (row["invoice_id"],)).fetchone()
        balance = invoice["gross_paise"] - invoice["credit_note_paise"] - cash.get(row["invoice_id"], 0) \
            - tds.get(row["invoice_id"], 0)
        if row["amount_paise"] > balance:
            raise DecisionError(f"{invoice['number']} no longer has that much open")
        implied = 0
        if chosen["kind"] == "tds_at_rate":
            implied = min(
                reconcile.tds_paise(invoice["taxable_paise"], chosen["tds_rate_bp"]), balance - row["amount_paise"]
            )
        # TDS counts as evidenced only when the supplier says so, separately from confirming the cash
        conn.execute(
            "UPDATE allocations SET status = 'confirmed', tds_reported_paise = ?, tds_evidenced_paise = ? "
            "WHERE id = ?",
            (implied, implied if options.get("accept_tds") else 0, row["id"]),
        )
    conn.execute(
        "UPDATE allocations SET status = 'rejected' WHERE payment_id = ? AND status = 'proposed'", (payment_id,)
    )


def decide_card(
    conn: sqlite3.Connection, card_id: str, decision: str, deps: Deps, now: datetime, note: str = "",
    decided_by: str = "supplier", options: dict | None = None,
) -> None:
    """Record a human decision, apply its effect and emit the event that re-runs the case.

    options: candidate, accept_tds (allocation); email (contact); close_dispute (dispute); pause / resume (any).
    """
    options = options or {}
    if decision not in DECISIONS:
        raise DecisionError(f"decision must be one of {', '.join(DECISIONS)}")
    with transaction(conn):
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if card is None:
            raise KeyError(card_id)
        if card["status"] != "open":
            raise DecisionError("this card has already been decided")
        case_id = card["case_id"]

        def human_evidence(field: str, value: dict, claim: str) -> None:
            insert_evidence(conn, Evidence(
                id=new_id("EV"), case_id=case_id, source_type="human", source_ref=card_id, ts=now, field=field,
                claim=claim, value=value, status=EvidenceStatus.VERIFIED,
            ))

        kind = card["kind"]
        if kind == "ambiguous_allocation":
            if decision == "approve":
                _confirm_allocation(conn, card, options, deps, now)
            elif decision == "reject":
                conn.execute(
                    "UPDATE allocations SET status = 'rejected' WHERE status = 'proposed' AND payment_id = ?",
                    (json.loads(card["payload"]).get("payment_id"),),
                )
        elif kind == "low_conf_contact" and decision == "approve":
            email = str(options.get("email", ""))
            if not conn.execute(
                "SELECT 1 FROM contacts ct JOIN invoices i ON i.customer_id = ct.customer_id "
                "JOIN cases c ON c.invoice_id = i.id WHERE c.id = ? AND lower(ct.email) = lower(?)",
                (case_id, email),
            ).fetchone():
                raise DecisionError("pick one of this buyer's addresses with options.email")
            human_evidence("contact_confirmed", {"email": email}, f"Supplier confirmed {email}")
        elif kind == "dispute_dossier" and decision == "approve" and options.get("close_dispute"):
            human_evidence("dispute_closed", {}, "Supplier marked the dispute resolved")
        elif kind == "out_of_policy_request" and decision == "approve":
            promise, _ = loader.load_promise(conn, case_id)
            if promise and promise.concessions:
                human_evidence("concession_approved", {"codes": promise.concessions},
                               f"Supplier allowed: {', '.join(promise.concessions)}")

        if options.get("pause") or options.get("resume"):
            conn.execute("UPDATE cases SET human_pause = ? WHERE id = ?", (int(bool(options.get("pause"))), case_id))

        decided_at = utc_iso(now)
        conn.execute(
            "UPDATE cards SET status = ?, decided_by = ?, decided_at = ?, note = ? WHERE id = ?",
            (DECISIONS[decision], decided_by, decided_at, note, card_id),
        )
        events.append(
            conn, "human_decision", "supplier", {"card_id": card_id, "decision": decision},
            events.card_key(card_id, decision, decided_at), case_id, ts=now,
        )
