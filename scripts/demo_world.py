"""The six demo cases as database rows and inbound events. All names and data are fictional.

ponytail: a stand-in until lane C's data/demo files and lane B's extraction and diagnosis exist.
Evidence is written already extracted, and DIAGNOSES is scripted where diagnose.propose will go.
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dlra import service
from dlra.models import CaseBundle, Diagnosis, Evidence, Payment

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Friday, in business hours

DIAGNOSES = {
    "K-1041": "PAPERWORK",
    "K-1052": "DISPUTE",
    "K-882": "CASH_CONSTRAINT",
    "K-1060": "PAID_UNMATCHED",
    "K-1077": "SILENT_UNKNOWN",
    "K-1089": "SILENT_UNKNOWN",
}
CONTEXT_FIELDS = {"reply_from"}  # history, not the reason an invoice is unpaid


def scripted_diagnosis(bundle: CaseBundle) -> Diagnosis:
    return Diagnosis(
        state=DIAGNOSES[bundle.case_id], confidence=0.85,
        evidence_ids=[e.id for e in bundle.evidence if e.field not in CONTEXT_FIELDS],
        rationale="scripted stand-in for diagnose.propose",
    )


def ev(case_id, n, source_type, field, claim, value=None, days_ago=3, status="verified") -> Evidence:
    return Evidence(
        id=f"E-{case_id}-{n}", case_id=case_id, source_type=source_type, source_ref=f"{source_type}:{n}",
        ts=NOW - timedelta(days=days_ago), field=field, claim=claim, value=value, status=status,
    )


def _customer(conn, cid, name, email, rate=0, basis="none", history=None):
    conn.execute(
        "INSERT INTO customers (id, canonical_name, tds_rate_bp, tds_basis, payment_history) VALUES (?, ?, ?, ?, ?)",
        (cid, name, rate, basis, json.dumps(history or {})),
    )
    _contact(conn, cid, email)


def _contact(conn, cid, email, source="ledger"):
    conn.execute(
        "INSERT INTO contacts (id, customer_id, email, source, status) VALUES (?, ?, ?, ?, 'unverified')",
        (f"CT-{cid}-{source}", cid, email, source),
    )


def _invoice(conn, number, cid, taxable, gross):
    conn.execute(
        "INSERT INTO invoices (id, number, customer_id, issue_date, due_date, taxable_paise, gst_paise, gross_paise) "
        "VALUES (?, ?, ?, '2026-10-01', '2026-11-01', ?, ?, ?)",
        (number, number, cid, taxable * 100, (gross - taxable) * 100, gross * 100),
    )


def _case(conn, case_id, invoice_id, reply_from=None):
    conn.execute("INSERT INTO cases (id, invoice_id) VALUES (?, ?)", (case_id, invoice_id))
    if reply_from:  # the earlier thread that makes the ledger contact verified
        service.insert_evidence(conn, ev(case_id, 0, "email", "reply_from", f"Replied from {reply_from} in August",
                                         {"email": reply_from}, days_ago=100))


def seed(conn) -> None:
    _customer(conn, "KAV", "Kaveri Agro Foods", "accounts@kaveri-agro.example")
    _invoice(conn, "INV-1041", "KAV", 100000, 118000)
    _case(conn, "K-1041", "INV-1041", "accounts@kaveri-agro.example")

    _customer(conn, "DEC", "Deccan Hardware Mart", "pay@deccan-hardware.example")
    _invoice(conn, "INV-1052", "DEC", 73220, 86400)
    _case(conn, "K-1052", "INV-1052", "pay@deccan-hardware.example")

    _customer(conn, "NAR", "Narmada Polymers", "finance@narmada-polymers.example")
    _invoice(conn, "INV-882", "NAR", 355932, 420000)
    _case(conn, "K-882", "INV-882", "finance@narmada-polymers.example")

    _customer(conn, "SAH", "Sahyadri Retail Pvt Ltd", "accounts@sahyadri-retail.example", 200, "contract_work",
              {"tds_deducted_ratio": 1.0, "n_payments": 6})
    for number, taxable, gross in [("INV-1060", 158500, 187030), ("INV-1061", 125000, 147500),
                                   ("INV-1062", 100000, 118000), ("INV-1063", 218500, 257830)]:
        _invoice(conn, number, "SAH", taxable, gross)
    _case(conn, "K-1060", "INV-1060", "accounts@sahyadri-retail.example")

    _customer(conn, "GOD", "Godavari Electricals", "accounts@godavari-elec.example")
    _contact(conn, "GOD", "godavari.accts@mailbox.example", "email")
    _contact(conn, "GOD", "accounts@godavari-elec.example", "olostep")
    _invoice(conn, "INV-1077", "GOD", 54237, 64000)
    _case(conn, "K-1077", "INV-1077", "accounts@godavari-elec.example")

    _customer(conn, "KON", "Konkan Marine Supplies", "ap@konkan-marine.example",
              history={"on_time_ratio": 0.92, "n_payments": 36})
    _invoice(conn, "INV-1089", "KON", 203390, 240000)
    _case(conn, "K-1089", "INV-1089", "ap@konkan-marine.example")


def inputs() -> list[tuple[str, str, str, str, list[Evidence]]]:
    """(label, event kind, source, case id, evidence) in demo order."""
    return [
        ("1 missing PO", "email_received", "email", "K-1041", [
            ev("K-1041", 1, "pdf", "po_number", "Buyer's Order No. is blank on INV-1041"),
            ev("K-1041", 2, "email", "paperwork_gap", "'Can't process, PO number missing on invoice'"),
            ev("K-1041", 3, "pdf", "po_on_file", "PO-7781 is on file", {"po_number": "PO-7781"}),
        ]),
        ("2 damaged goods", "email_received", "email", "K-1052", [
            ev("K-1052", 1, "pdf", "dispute", "Delivery note: 4 of 30 cartons damaged", {"reason": "quality"}),
        ]),
        ("3 fee-waiver promise", "email_received", "email", "K-882", [
            ev("K-882", 1, "email", "promise_proposal",
               "'We'll pay half next week and the rest after month end if you drop the late fee.'",
               {"invoice_ids": ["INV-882"],
                "installments": [{"share": 0.5, "due_text": "next week"}, {"due_text": "after month end"}],
                "conditions": ["waive late fee"]}),
        ]),
        ("4 unreferenced transfer", "buyer_submission", "buyer_page", "K-1060", [
            ev("K-1060", 1, "buyer_page", "payment_report", "Buyer reports paying, TDS ₹7,670 deducted",
               {"amount_paise": 44486000, "paid_on": "2026-11-20", "tds_paise": 767000}, days_ago=1,
               status="unverified"),
        ]),
        ("5 conflicting contact", "email_received", "email", "K-1077", [
            ev("K-1077", 1, "email", "contact_change",
               "'Our accounts email has changed, please send all invoices and bank details here'",
               status="unverified"),
        ]),
        ("6 distress signal", "evidence_changed", "crustdata", "K-1089", [
            ev("K-1089", 1, "ledger", "overdue", "21 days overdue, no reply"),
            ev("K-1089", 2, "crustdata", "distress_signal", "Headcount down 30%, negative news item",
               {"headcount_change_pct": -30}, status="unverified"),
        ]),
    ]


def queue_old_reminder(conn) -> None:
    """What a calendar-based tool had waiting in the outbox when the dispute arrived."""
    conn.execute(
        "INSERT INTO messages (id, case_id, contact_id, action, intent, subject, body, status) VALUES "
        "('MSG-OLD', 'K-1052', 'CT-DEC-ledger', 'PAYMENT_REMINDER', 'payment_reminder', "
        "'Invoice INV-1052', 'A reminder about invoice INV-1052.', 'queued')"
    )


def bank_line() -> Payment:
    return Payment(id="PAY-1", amount_paise=44486000, value_date=date(2026, 11, 20), narration="NEFT-XYZ",
                   payer_name="SAHYADRI RETAIL")

