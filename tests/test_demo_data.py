"""data/demo must agree with what the engine's demo world assumes, and with its own source file."""
import csv
import json
from collections import Counter
from datetime import timedelta
from email import policy as email_policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml
from openpyxl import load_workbook
from pypdf import PdfReader

from dlra import engine
from dlra.db import connect
from dlra.models import Action, State
from scripts import demo_world
from scripts.make_demo_data import DEMO, build

SPEC = Path(__file__).resolve().parent.parent / "eval" / "scenario_spec.yaml"


def sheet(book, name):
    rows = book[name].iter_rows(values_only=True)
    header = next(rows)
    return [dict(zip(header, row, strict=True)) for row in rows]


def mail(path):
    return BytesParser(policy=email_policy.default).parsebytes(path.read_bytes())


def pdf_text(name):
    return PdfReader(DEMO / "pdfs" / f"{name}.pdf").pages[0].extract_text()


def test_committed_files_are_what_the_generator_writes(tmp_path):
    build(tmp_path)

    def files(root):
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file() and p.name != "world.yaml"}

    fresh, committed = files(tmp_path), files(DEMO)
    assert fresh.keys() == committed.keys()
    for name, path in fresh.items():
        if name.endswith(".xlsx"):
            a, b = load_workbook(path), load_workbook(committed[name])
            assert a.sheetnames == b.sheetnames
            for title in a.sheetnames:
                assert sheet(a, title) == sheet(b, title)
        elif name.endswith(".pdf"):  # compressed bytes can differ between zlib builds; the text must not
            assert PdfReader(path).pages[0].extract_text() == PdfReader(committed[name]).pages[0].extract_text(), name
        else:
            assert path.read_bytes() == committed[name].read_bytes(), name


def test_ledger_matches_the_demo_world():
    conn = connect()
    demo_world.seed(conn)
    book = load_workbook(DEMO / "ledger" / "ledger.xlsx")
    sales = {r["Voucher No."]: r for r in sheet(book, "Vouchers") if r["Voucher Type"] == "Sales"}
    for row in conn.execute(
        "SELECT i.*, c.canonical_name, c.tds_rate_bp, ct.email FROM invoices i "
        "JOIN customers c ON c.id = i.customer_id "
        "JOIN contacts ct ON ct.customer_id = c.id AND ct.source = 'ledger'"
    ):
        sale = sales[row["number"]]
        assert (sale["Party Name"], sale["Debit"] * 100, sale["Taxable Value"] * 100, sale["GST"] * 100) == (
            row["canonical_name"], row["gross_paise"], row["taxable_paise"], row["gst_paise"])
        assert sale["Taxable Value"] + sale["GST"] == sale["Debit"]
        assert abs(sale["GST"] - sale["Taxable Value"] * 0.18) <= 1
        party = next(p for p in sheet(book, "Party Master") if p["Party Name"] == row["canonical_name"])
        assert (party["Email"], party["TDS Rate (bp)"]) == (row["email"], row["tds_rate_bp"])


def test_ledger_history_supports_cases_4_and_6():
    vouchers = sheet(load_workbook(DEMO / "ledger" / "ledger.xlsx"), "Vouchers")

    def settled(party):
        mine = [r for r in vouchers if r["Party Name"] == party]
        by_no = {r["Voucher No."]: r for r in mine}
        return [(r, by_no[f"R-{r['Voucher No.']}"], by_no.get(f"J-{r['Voucher No.']}"))
                for r in mine if r["Voucher Type"] == "Sales" and f"R-{r['Voucher No.']}" in by_no]

    sahyadri = settled("Sahyadri Retail Pvt Ltd")
    assert len(sahyadri) == 6
    for sale, receipt, journal in sahyadri:
        assert journal["Credit"] == sale["Taxable Value"] * 2 // 100
        assert receipt["Credit"] + journal["Credit"] == sale["Debit"]
    konkan = settled("Konkan Marine Supplies")
    on_time = sum(receipt["Date"] <= sale["Due Date"] for sale, receipt, _ in konkan)
    assert (on_time, len(konkan)) == (33, 36)  # 92%
    assert max(receipt["Date"] for _, receipt, _ in konkan).date() < demo_world.NOW.date()
    assert konkan[0][0]["Date"].year == 2023


def test_case_6_is_21_days_overdue():
    book = load_workbook(DEMO / "ledger" / "ledger.xlsx")
    sale = next(r for r in sheet(book, "Vouchers") if r["Voucher No."] == "INV-1089")
    assert demo_world.NOW.date() - sale["Due Date"].date() == timedelta(days=21)


def test_every_ledger_contact_has_replied_2_to_6_months_ago():
    parties = sheet(load_workbook(DEMO / "ledger" / "ledger.xlsx"), "Party Master")
    replies = [mail(p) for p in (DEMO / "emails").glob("earlier-*.eml")]
    for party in parties:
        reply = next(m for m in replies if m["From"] == party["Email"])
        assert reply["In-Reply-To"]
        assert 60 <= (demo_world.NOW - parsedate_to_datetime(reply["Date"])).days <= 183


def test_every_address_is_synthetic():
    for path in (DEMO / "emails").glob("*.eml"):
        message = mail(path)
        assert all(message[h].endswith(".example") for h in ("From", "To")), path.name


def test_case_1_invoice_has_a_blank_po_box_and_the_po_is_on_file():
    text = pdf_text("INV-1041")
    assert "Buyer's Order No." in text and "PO-7781" not in text
    # nothing but whitespace between the label, its "Dated" and the next label
    assert not text.split("Buyer's Order No.")[1].split("Mode/Terms")[0].replace("Dated", "").strip()
    assert "PO-7781" in pdf_text("PO_PO-7781")
    assert "SRPL/FIT/091" in pdf_text("INV-1060")  # the other invoices do carry a PO
    assert "4 of 30 cartons damaged" in pdf_text("DN-5052")


def test_the_emails_carry_the_lines_the_cases_turn_on():
    def body(case):
        return mail(DEMO / "emails" / f"{case}.eml").get_content()

    assert "PO number missing on invoice" in body("K-1041")
    assert "pay half next week and the rest after month end" in body("K-882")
    assert "accounts email has changed" in body("K-1077")
    assert mail(DEMO / "emails" / "K-1077.eml")["From"] == "godavari.accts@mailbox.example"
    assert not (DEMO / "emails" / "K-1089.eml").exists()  # case 6 is silent


def test_bank_line_report_and_fixtures_match_the_demo_world():
    with (DEMO / "bank" / "statement.csv").open(encoding="utf-8", newline="") as handle:
        deposits = [r for r in csv.DictReader(handle) if r["Deposit"]]
    line = demo_world.bank_line()
    assert [(r["Date"], r["Narration"], r["Remitter"], int(r["Deposit"]) * 100) for r in deposits] == [
        (str(line.value_date), line.narration, line.payer_name, line.amount_paise)]
    report = json.loads((DEMO / "buyer_page" / "K-1060-payment-report.json").read_text(encoding="utf-8"))
    assert (report["amount_paise"], report["tds_paise"]) == (44486000, 767000)
    olostep = json.loads((DEMO / "signals" / "olostep" / "K-1077.json").read_text(encoding="utf-8"))
    assert olostep["emails"] == ["accounts@godavari-elec.example"]


def test_scenario_spec_adds_up_and_uses_the_engines_names():
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    templates = spec["templates"]
    assert len({t["id"] for t in templates}) == len(templates)
    assert sum(t["count"] for t in templates) == spec["total"] == 45

    per_state, tags = Counter(), Counter()
    for t in templates:
        per_state[State(t["state"])] += t["count"]
        tags.update({tag: t["count"] for tag in t.get("tags", [])})
    assert set(per_state) == set(State)
    minimums = {"tds": 4, "tds_goods_0_1": 1, "multi_invoice": 4, "unreferenced": 3, "partial": 3,
                "conflicting_contact": 4, "weak_signal": 3, "blank_po_tally": 2, "payment_never_arrives": 2}
    assert {tag: tags[tag] for tag in minimums if tags[tag] < minimums[tag]} == {}

    engine_source = Path(engine.__file__).read_text(encoding="utf-8")
    for t in templates:
        labels = t["labels"]
        Action(labels["allowed_action"])
        State(labels.get("final_state", t["state"]))
        assert labels["expected_outreach_count"] == len(labels["expected_intents"]), t["id"]
        for name in (*labels["expected_intents"], *labels["cards"]):
            assert name in engine_source, f"{t['id']}: the engine has no '{name}'"
