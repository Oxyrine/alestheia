"""Generate the synthetic demo sources under data/demo/ from data/demo/world.yaml.

    uv run python -m scripts.make_demo_data [--out DIR]

CSV, JSON and mail output is byte-for-byte repeatable. The PDFs repeat on one machine (compression can
differ between zlib builds). The XLSX repeats cell for cell, but openpyxl stamps the save time.
"""
import argparse
import csv
import json
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import yaml
from fpdf import FPDF
from openpyxl import Workbook

DEMO = Path(__file__).resolve().parent.parent / "data" / "demo"
TERMS_DAYS = 30
STAMP = datetime(2026, 11, 27, tzinfo=UTC)
LEDGER_COLUMNS = ["Date", "Voucher No.", "Voucher Type", "Party Name", "GSTIN", "Debit", "Credit",
                  "Taxable Value", "GST", "Due Date", "Narration", "Ref (PO)"]
PARTY_COLUMNS = ["Party Name", "GSTIN", "City", "Email", "TDS Rate (bp)", "TDS Basis"]
BANK_COLUMNS = ["Date", "Narration", "Remitter", "Chq/Ref No", "Withdrawal", "Deposit", "Balance"]


def inr(rupees: int) -> str:
    """The core PDF fonts have no rupee sign."""
    digits = str(rupees)
    head, tail, groups = digits[:-3], digits[-3:], []
    while head:
        head, groups = head[:-2], [head[-2:], *groups]
    return "INR " + ",".join([*groups, tail])


def due(issued: date) -> date:
    return issued + timedelta(days=TERMS_DAYS)


def history_rows(world: dict):
    """Ledger vouchers for the settled invoices: sale, receipt and, where deducted, a TDS journal."""
    for cid, spec in world["history"].items():
        party = world["customers"][cid]
        for index in range(spec["count"]):
            number = f"INV-{spec['first_number'] + index:04d}"
            issued = spec["first_issued"] + timedelta(days=spec["every_days"] * index)
            taxable = spec["taxable"] + index * 500
            gst = round(taxable * 0.18)
            tds = taxable * party["tds_rate_bp"] // 10_000 if spec["deducts_tds"] else 0
            paid_on = due(issued) + timedelta(days=12 if index in spec["late"] else -2)
            yield [issued, number, "Sales", party["name"], party["gstin"], taxable + gst, None, taxable, gst,
                   due(issued), "", ""]
            yield [paid_on, f"R-{number}", "Receipt", party["name"], party["gstin"], None, taxable + gst - tds,
                   None, None, None, f"Against {number}", ""]
            if tds:
                yield [paid_on, f"J-{number}", "Journal", party["name"], party["gstin"], None, tds,
                       None, None, None, f"TDS deducted against {number}", ""]


def write_ledger(world: dict, out: Path) -> None:
    rows = list(history_rows(world))
    for inv in world["invoices"]:
        party = world["customers"][inv["customer"]]
        rows.append([inv["issued"], inv["number"], "Sales", party["name"], party["gstin"], inv["gross"], None,
                     inv["taxable"], inv["gross"] - inv["taxable"], due(inv["issued"]), inv["item"], inv["po"] or ""])
    book = Workbook()
    book.properties.created = STAMP.replace(tzinfo=None)
    vouchers = book.active
    vouchers.title = "Vouchers"
    vouchers.append(LEDGER_COLUMNS)
    for row in sorted(rows, key=lambda r: (r[0], r[1])):
        vouchers.append(row)
    parties = book.create_sheet("Party Master")
    parties.append(PARTY_COLUMNS)
    for c in world["customers"].values():
        parties.append([c["name"], c["gstin"], c["city"], c["email"], c["tds_rate_bp"], c["tds_basis"]])
    for sheet in book:
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = 18
    book.save(out / "ledger" / "ledger.xlsx")


def write_bank(world: dict, out: Path) -> None:
    balance = world["bank"]["opening_balance"]
    with (out / "bank" / "statement.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(BANK_COLUMNS)
        for line in world["bank"]["lines"]:
            withdrawal, deposit = line.get("withdrawal", 0), line.get("deposit", 0)
            balance += deposit - withdrawal
            writer.writerow([line["date"], line["narration"], line.get("remitter", ""), line["ref"],
                             withdrawal or "", deposit or "", balance])


def _pdf(path: Path, title: str, boxes: list[list[tuple[str, str]]], lines: list[str]) -> None:
    """`boxes` are rows of (label, value) cells, drawn the way Tally lays out its header grid."""
    pdf = FPDF()
    pdf.set_creation_date(STAMP)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", size=9)
    for row in boxes:
        width, top = pdf.epw / len(row), pdf.get_y()
        for index, (label, value) in enumerate(row):
            pdf.set_xy(pdf.l_margin + index * width, top)
            pdf.multi_cell(width, 6, f"{label}\n{value or ' '}", border=1)
        pdf.set_xy(pdf.l_margin, top + 12)
    pdf.ln(6)
    pdf.set_font("Helvetica", size=10)
    for line in lines:
        pdf.multi_cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=8)
    pdf.ln(8)
    pdf.cell(0, 5, "Synthetic document. Every business named here is fictional.")
    pdf.output(path)


def slug(number: str) -> str:
    return number.replace("/", "-")


def write_pdfs(world: dict, out: Path) -> None:
    supplier, customers = world["supplier"], world["customers"]
    invoices = {i["number"]: i for i in world["invoices"]}
    po_dates = {p["number"]: p["dated"] for p in world["documents"]["purchase_orders"]}

    def parties(cid: str) -> list[str]:
        c = customers[cid]
        return [f"Supplier: {supplier['name']}, {supplier['city']}. GSTIN {supplier['gstin']}",
                f"Buyer: {c['name']}, {c['city']}. GSTIN {c['gstin']}"]

    for inv in world["invoices"]:
        po = inv["po"] or ""
        dated = str(po_dates.get(po, inv["issued"] - timedelta(days=5))) if po else ""
        _pdf(
            out / "pdfs" / f"{inv['number']}.pdf", "Tax Invoice",
            [[("Invoice No.", inv["number"]), ("Dated", str(inv["issued"]))],
             [("Buyer's Order No.", po), ("Dated", dated)],
             [("Mode/Terms of Payment", f"Net {TERMS_DAYS} days"), ("Due Date", str(due(inv["issued"])))]],
            [*parties(inv["customer"]), "", f"{inv['item']}, quantity {inv['qty']}",
             f"Taxable value: {inr(inv['taxable'])}", f"GST 18%: {inr(inv['gross'] - inv['taxable'])}",
             f"Total: {inr(inv['gross'])}"],
        )
    for order in world["documents"]["purchase_orders"]:
        _pdf(
            out / "pdfs" / f"PO_{slug(order['number'])}.pdf", "Purchase Order",
            [[("Purchase Order No.", order["number"]), ("Dated", str(order["dated"]))]],
            [*parties(order["customer"]), "", f"{order['item']}, quantity {order['qty']}",
             f"Order value including GST: {inr(order['amount'])}",
             "Quote this purchase order number on your invoice."],
        )
    for note in world["documents"]["delivery_notes"]:
        inv = invoices[note["invoice"]]
        _pdf(
            out / "pdfs" / f"{note['number']}.pdf", "Delivery Acknowledgement",
            [[("Delivery Note No.", note["number"]), ("Dated", str(note["dated"]))],
             [("Invoice No.", note["invoice"]), ("Buyer's Order No.", note["po"])]],
            [*parties(inv["customer"]), "", f"{inv['item']}, quantity {inv['qty']}",
             f"Receiver's remarks: {note['remarks']}", "Signed: receiving clerk (fictional)"],
        )


def _eml(path: Path, sender: str, to: str, when: str, subject: str, body: str, reply_to: str | None = None) -> None:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, to, subject
    message["Date"] = format_datetime(datetime.fromisoformat(when))
    message["Message-ID"] = f"<{path.stem}@{sender.rsplit('@', 1)[1]}>"
    if reply_to:
        message["In-Reply-To"] = reply_to
    message.set_content(body + "\n")
    path.write_bytes(message.as_bytes())


def write_emails(world: dict, out: Path) -> None:
    supplier = world["supplier"]["email"]
    domain = supplier.rsplit("@", 1)[1]
    for cid, reply in world["earlier_replies"].items():
        _eml(out / "emails" / f"earlier-{cid}.eml", world["customers"][cid]["email"], supplier,
             reply["date"], reply["subject"], reply["body"], reply_to=f"<thread-{cid}@{domain}>")
    for mail in world["case_emails"]:
        _eml(out / "emails" / f"{mail['case']}.eml", mail["from"], supplier, mail["date"], mail["subject"],
             mail["body"])


def _json(path: Path, data: dict) -> None:
    path.write_bytes((json.dumps(data, indent=2, default=str) + "\n").encode())


def write_json(world: dict, out: Path) -> None:
    for report in world["buyer_reports"]:
        _json(out / "buyer_page" / f"{report['case']}-payment-report.json",
              {"case": report["case"], "submitted": report["submitted"], "amount_paise": report["amount"] * 100,
               "paid_on": report["paid_on"], "tds_paise": report["tds"] * 100, "simulated": True})
    for provider, items in world["signals"].items():
        for item in items:
            _json(out / "signals" / provider / f"{item['case']}.json", {"synthetic_fixture": True, **item})


def build(out: Path = DEMO, source: Path = DEMO / "world.yaml") -> None:
    world = yaml.safe_load(source.read_text(encoding="utf-8"))
    for folder in ("ledger", "bank", "pdfs", "emails", "buyer_page", "signals/crustdata", "signals/olostep"):
        (out / folder).mkdir(parents=True, exist_ok=True)
    for write in (write_ledger, write_bank, write_pdfs, write_emails, write_json):
        write(world, out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEMO)
    build(parser.parse_args().out)


if __name__ == "__main__":
    main()
