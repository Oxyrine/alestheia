"""Database rows -> CaseBundle. Every flag the engine acts on is derived here, by code.

Evidence contract for whoever writes evidence (extraction, buyer page, integrations).
`field` -> `value`:

    payment_report       {amount_paise, paid_on, utr?, tds_paise?, invoice_numbers?, remittance_invoice_numbers?}
    dispute              {reason}
    dispute_closed       {}                 (human decision only)
    po_on_file           {po_number}        (our records hold the item the buyer says is missing)
    distress_signal      {...}              (crustdata / olostep)
    promise_proposal     shape of promises.build_promise
    promise_confirmation {due_texts: [...], conditions: [...]}
    reply_from           {email}            (the buyer has replied from this address)
    contact_confirmed    {email}            (human decision only)
    concession_approved  {codes: [...]}     (human decision only)

An extraction whose quote is not found in the source must NOT be written under these fields.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta

from dlra import reconcile
from dlra.db import utc_iso
from dlra.gates import resolve_contacts
from dlra.models import (
    CaseBundle,
    CaseFacts,
    Contact,
    Customer,
    Evidence,
    EvidenceStatus,
    Invoice,
    Payment,
    Promise,
    PromiseStatus,
    State,
)

LIVE = (EvidenceStatus.UNVERIFIED, EvidenceStatus.VERIFIED)
HUMAN_ONLY = {"dispute_closed", "contact_confirmed", "concession_approved"}


def _invoice(row: sqlite3.Row) -> Invoice:
    return Invoice(**{k: row[k] for k in Invoice.model_fields})


def _customer(row: sqlite3.Row) -> Customer:
    data = {k: row[k] for k in Customer.model_fields}
    data["aliases"] = json.loads(row["aliases"] or "[]")
    data["payment_history"] = json.loads(row["payment_history"] or "{}")
    data["entity_type"] = data["entity_type"] or "company"
    data["tds_basis"] = data["tds_basis"] or "none"
    return Customer(**data)


def _evidence(row: sqlite3.Row) -> Evidence:
    data = dict(row)
    data["value"] = json.loads(row["value"]) if row["value"] else None
    return Evidence(**data)


def case_evidence(conn: sqlite3.Connection, case_id: str) -> list[Evidence]:
    rows = conn.execute("SELECT * FROM evidence WHERE case_id = ? ORDER BY ts, id", (case_id,))
    return [_evidence(r) for r in rows]


def confirmed_totals(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int]]:
    """(cash, evidenced TDS) per invoice id, from confirmed allocations only."""
    cash, tds = {}, {}
    for row in conn.execute(
        "SELECT invoice_id, SUM(amount_paise) AS cash, SUM(tds_evidenced_paise) AS tds FROM allocations "
        "WHERE status = 'confirmed' GROUP BY invoice_id"
    ):
        cash[row["invoice_id"]], tds[row["invoice_id"]] = row["cash"], row["tds"]
    return cash, tds


def customer_payments(conn: sqlite3.Connection, customer: Customer) -> list[Payment]:
    """This buyer's payments that are not yet confirmed against an invoice."""
    customers = [_customer(r) for r in conn.execute("SELECT * FROM customers")]
    invoices = [_invoice(r) for r in conn.execute("SELECT * FROM invoices")]
    out = []
    for row in conn.execute(
        "SELECT * FROM payments WHERE id NOT IN (SELECT payment_id FROM allocations WHERE status = 'confirmed') "
        "ORDER BY value_date, id"
    ):
        payment = _payment(row)
        cited = reconcile.cited_numbers(payment.narration, invoices)
        owners = {i.customer_id for i in invoices if i.number in cited}
        if len(owners) == 1:
            owner = owners.pop()
        else:
            resolved = reconcile.resolve_payer(payment, customers)
            owner = resolved.id if resolved else None
        if owner == customer.id:
            out.append(payment)
    return out


def reconciled_payments(conn: sqlite3.Connection, invoice_id: str) -> list[Payment]:
    """Payments already confirmed against this invoice."""
    rows = conn.execute(
        "SELECT DISTINCT p.* FROM payments p JOIN allocations a ON a.payment_id = p.id "
        "WHERE a.invoice_id = ? AND a.status = 'confirmed'", (invoice_id,)
    )
    return [_payment(r) for r in rows]


def _payment(row: sqlite3.Row) -> Payment:
    data = {k: row[k] for k in Payment.model_fields}
    data["narration"], data["payer_name"] = data["narration"] or "", data["payer_name"] or ""
    data["source"] = data["source"] or "bank"
    return Payment(**data)


def payment_matches_report(report: dict, payments: list[Payment], tolerance_paise: int = 100) -> bool:
    since = date.fromisoformat(report["paid_on"]) - timedelta(days=7) if report.get("paid_on") else date.min
    def matches(p: Payment) -> bool:
        if report.get("utr") and p.utr:
            return p.utr == report["utr"]
        return p.value_date >= since and abs(p.amount_paise - report.get("amount_paise", -10**12)) <= tolerance_paise

    return any(matches(p) for p in payments)


def load_promise(conn: sqlite3.Connection, case_id: str) -> tuple[Promise | None, int]:
    row = conn.execute(
        "SELECT data, baseline_paise FROM promises WHERE case_id = ? ORDER BY rowid DESC LIMIT 1", (case_id,)
    ).fetchone()
    if not row:
        return None, 0
    return Promise.model_validate_json(row["data"]), row["baseline_paise"]


def _facts(conn, case, invoice, customer, evidence, payments, promise, cash, tds, now, exclude_message_id):
    def of(name: str, statuses=LIVE) -> list[Evidence]:
        return [
            e for e in evidence
            if e.field == name and e.status in statuses and (name not in HUMAN_ONLY or e.source_type == "human")
        ]

    case_id = case["id"]
    done = reconciled_payments(conn, invoice.id)
    # a report is spent only when the payment it describes has been confirmed, not on any earlier one
    reports = [r for r in of("payment_report") if not payment_matches_report(r.value or {}, done)]
    expired = of("payment_report", (EvidenceStatus.EXPIRED_UNVERIFIED,))
    decided_not_found = conn.execute(
        "SELECT 1 FROM cards WHERE case_id = ? AND kind = 'reported_payment_not_found' AND status != 'open'",
        (case_id,),
    ).fetchone()
    # a second report after an unsupported one earns no new silence window unless the money is there
    supported = [r for r in reports if payment_matches_report(r.value or {}, payments)]
    live_reports = supported if expired else reports
    latest = (live_reports[-1].value or {}) if live_reports else {}

    closed_at = max((e.ts for e in of("dispute_closed")), default=None)
    disputes = [e for e in of("dispute") if closed_at is None or e.ts > closed_at]

    week_ago = utc_iso(now - timedelta(days=7))
    sent_this_week = conn.execute(
        "SELECT COUNT(*) FROM messages m JOIN cases c ON c.id = m.case_id JOIN invoices i ON i.id = c.invoice_id "
        "WHERE i.customer_id = ? AND m.status = 'sent' AND m.sent_at >= ?",
        (customer.id, week_ago),
    ).fetchone()[0]
    messages = conn.execute(
        "SELECT action, intent FROM messages WHERE case_id = ? AND status IN ('queued', 'sent') AND id != ?",
        (case_id, exclude_message_id or ""),
    ).fetchall()
    confirmed_ids = [
        r[0] for r in conn.execute("SELECT DISTINCT payment_id FROM allocations WHERE status = 'confirmed'")
    ]
    proposals = of("promise_proposal")

    return CaseFacts(
        payment_reported=bool(live_reports),
        dispute_open=bool(disputes),
        human_pause=bool(case["human_pause"]),
        sent_this_week=sent_this_week,
        neutral_questions_sent=sum(m["action"] == "NEUTRAL_QUESTION_ONCE" for m in messages),
        sent_intents=[m["intent"] for m in messages if m["intent"]],
        payment_report_expired=bool(expired) and not supported and not decided_not_found,
        bank_ref_asks_since_expiry=sum(m["intent"] == "ask_bank_reference_after_expiry" for m in messages),
        deadline_missed=bool(promise and promise.status == PromiseStatus.BROKEN),
        distress_signal=bool(of("distress_signal")),
        correction_on_file=bool(of("po_on_file")),
        reported_tds_paise=latest.get("tds_paise"),
        reported_invoice_numbers=latest.get("invoice_numbers") or [],
        remittance_invoice_numbers=latest.get("remittance_invoice_numbers") or [],
        confirmed_paid=cash,
        tds_evidenced_paise=tds.get(invoice.id, 0),
        allocated_payment_ids=confirmed_ids,
        promise_extract=(proposals[-1].value if proposals and promise is None else None),
        approved_concessions=sorted({c for e in of("concession_approved") for c in e.value["codes"]}),
    )


def load_bundle(
    conn: sqlite3.Connection, case_id: str, now: datetime, exclude_message_id: str | None = None
) -> CaseBundle:
    case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        raise KeyError(case_id)
    invoice = _invoice(conn.execute("SELECT * FROM invoices WHERE id = ?", (case["invoice_id"],)).fetchone())
    customer = _customer(conn.execute("SELECT * FROM customers WHERE id = ?", (invoice.customer_id,)).fetchone())
    evidence = case_evidence(conn, case_id)
    cash, tds = confirmed_totals(conn)

    own = [_invoice(r) for r in conn.execute("SELECT * FROM invoices WHERE customer_id = ?", (customer.id,))]
    still_open = [
        i for i in own
        if i.gross_paise - i.credit_note_paise - cash.get(i.id, 0) - tds.get(i.id, 0) > 0
    ]

    # Contact status is re-derived from evidence on every load. A reply or a human pick on any
    # of this buyer's cases counts.
    emails = {"reply_from": set(), "contact_confirmed": set(), "opt_out": set()}
    for row in conn.execute(
        "SELECT e.field, e.value, e.source_type FROM evidence e JOIN cases c ON c.id = e.case_id "
        "JOIN invoices i ON i.id = c.invoice_id WHERE i.customer_id = ? "
        "AND e.field IN ('reply_from', 'contact_confirmed', 'opt_out') AND e.status IN ('verified', 'unverified')",
        (customer.id,),
    ):
        if row["field"] != "contact_confirmed" or row["source_type"] == "human":
            emails[row["field"]].add(json.loads(row["value"])["email"].casefold())
    contacts = [
        Contact(**{k: r[k] if r[k] is not None else Contact.model_fields[k].default for k in Contact.model_fields})
        for r in conn.execute("SELECT * FROM contacts WHERE customer_id = ? ORDER BY id", (customer.id,))
    ]
    contacts = [
        c.model_copy(update={"opted_out": c.opted_out or c.email.casefold() in emails["opt_out"]})
        for c in resolve_contacts(contacts, emails["reply_from"], emails["contact_confirmed"])
    ]

    payments = customer_payments(conn, customer)
    promise, _ = load_promise(conn, case_id)
    return CaseBundle(
        case_id=case_id, invoice=invoice, customer=customer, open_invoices=still_open, contacts=contacts,
        evidence=evidence, payments=payments, promise=promise,
        state=State(case["state"]) if case["state"] else None,
        facts=_facts(conn, case, invoice, customer, evidence, payments, promise, cash, tds, now, exclude_message_id),
    )
