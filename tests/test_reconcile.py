"""Every matcher case in the plan. Asserts outcome class, order and money, not exact scores."""
from datetime import date

import pytest

from dlra.gates import g_pay
from dlra.models import Customer, Invoice, Payment, fmt_inr
from dlra.reconcile import cited_numbers, match, open_balance, resolve_payer, settlement, tds_paise

DUE = date(2026, 11, 1)
R = 100  # paise per rupee


def inv(number, taxable, gross=None, credit=0, due=DUE, customer="C1"):
    gross = gross or taxable * 118 // 100
    return Invoice(
        id=number, number=number, customer_id=customer, issue_date=date(2026, 10, 1), due_date=due,
        taxable_paise=taxable * R, gst_paise=(gross - taxable) * R, gross_paise=gross * R,
        credit_note_paise=credit * R,
    )


def customer(rate=200, ratio=None, n=6, name="Sahyadri Retail Pvt Ltd"):
    history = {} if ratio is None else {"tds_deducted_ratio": ratio, "n_payments": n}
    return Customer(id="C1", canonical_name=name, tds_rate_bp=rate, payment_history=history)


def pay(rupees, narration="NEFT-XYZ", payer="SAHYADRI RETAIL"):
    return Payment(id="P1", amount_paise=int(rupees * R), value_date=date(2026, 11, 20), narration=narration,
                   payer_name=payer)


SAHYADRI = [inv("INV-1060", 158500), inv("INV-1061", 125000), inv("INV-1062", 100000), inv("INV-1063", 218500)]
C1 = {"INV-1060", "INV-1061", "INV-1062"}
C2 = {"INV-1060", "INV-1063"}


def lines(result):
    return [set(c.lines) for c in result.candidates]


def assert_not_postable(result, invoices):
    by_id = {i.id: i for i in invoices}
    for c in result.candidates:
        assert not g_pay(c, sum(open_balance(by_id[i]) for i in c.lines)).allowed


def test_demo_figures():
    assert [i.gross_paise // R for i in SAHYADRI] == [187030, 147500, 118000, 257830]
    assert sum(tds_paise(i.taxable_paise, 200) for i in SAHYADRI[:3]) == 7670 * R


def test_goods_tds_is_not_a_partial(policy):
    invoices = [inv("INV-1", 100000)]
    got = match(pay(117900), invoices, customer(rate=10), policy.tds)
    assert [c.kind for c in got.candidates] == ["tds_at_rate"]
    assert got.candidates[0].tds_paise == 100 * R
    view = settlement(invoices[0].gross_paise, 117900 * R, tds_reported_paise=100 * R)
    assert (view.status, view.unexplained_paise) == ("settled_pending_tds_evidence", 100 * R)
    assert settlement(invoices[0].gross_paise, 117900 * R).status == "partially_settled"
    assert_not_postable(got, invoices)


def test_contract_tds(policy):
    got = match(pay(116000), [inv("INV-1", 100000)], customer(rate=200), policy.tds)
    assert (got.candidates[0].kind, got.candidates[0].tds_paise) == ("tds_at_rate", 2000 * R)
    assert_not_postable(got, [inv("INV-1", 100000)])


def test_rate_unknown_tries_standard_rates(policy):
    got = match(pay(116000), [inv("INV-1", 100000)], customer(rate=None), policy.tds)
    assert (got.candidates[0].kind, got.candidates[0].tds_rate_bp) == ("tds_at_rate", 200)
    assert "rate not on file" in got.candidates[0].basis
    assert_not_postable(got, [inv("INV-1", 100000)])


def test_case_4_with_report_favours_c1_and_posts_nothing(policy):
    got = match(pay(444860), SAHYADRI, customer(ratio=1.0), policy.tds, reported_tds_paise=7670 * R)
    assert got.outcome == "favoured"
    assert lines(got) == [C1, C2]
    assert [c.kind for c in got.candidates] == ["tds_at_rate", "exact_gross"]
    assert got.candidates[0].tds_paise == 7670 * R
    assert "₹7,670" in got.reason
    assert got.candidates[0].lines == {"INV-1060": 183860 * R, "INV-1061": 145000 * R, "INV-1062": 116000 * R}
    assert_not_postable(got, SAHYADRI)
    view = settlement(452530 * R, 444860 * R, tds_reported_paise=7670 * R)
    assert (view.cash_received_paise, view.tds_evidenced_paise, view.unexplained_paise) == (444860 * R, 0, 7670 * R)


def test_case_4_no_report_consistent_history_only_leans(policy):
    got = match(pay(444860), SAHYADRI, customer(ratio=1.0), policy.tds)
    assert (got.outcome, got.lean) == ("ambiguous", "leans: usual 2% deduction")
    assert lines(got) == [C1, C2]


def test_case_4_no_report_mixed_history(policy):
    got = match(pay(444860), SAHYADRI, customer(ratio=0.5), policy.tds)
    assert (got.outcome, got.lean) == ("ambiguous", None)
    assert sorted(map(sorted, lines(got))) == sorted(map(sorted, [C1, C2]))
    assert_not_postable(got, SAHYADRI)


def test_unreferenced_matches_by_amount(policy):
    got = match(pay(118000), [inv("INV-1", 100000)], customer(rate=0), policy.tds)
    assert got.candidates[0].kind == "exact_gross" and not got.candidates[0].cites_invoice
    assert_not_postable(got, [inv("INV-1", 100000)])


def test_partial_halves_the_balance_and_keeps_the_case_open(policy):
    invoice = inv("INV-1", 100000)
    got = match(pay(59000), [invoice], customer(rate=0), policy.tds)
    assert [c.kind for c in got.candidates] == ["partial"]
    assert open_balance(invoice, {"INV-1": 59000 * R}) == 59000 * R
    assert settlement(invoice.gross_paise, 59000 * R).status == "partially_settled"


def test_credit_note_with_reference_is_postable(policy):
    invoice = inv("INV-1041", 100000, credit=11800)
    got = match(pay(106200, narration="NEFT INV-1041"), [invoice], customer(rate=0), policy.tds)
    assert (got.outcome, got.candidates[0].kind) == ("reference", "exact_gross")
    assert g_pay(got.candidates[0], open_balance(invoice)).allowed


def test_reference_with_unevidenced_tds_is_not_postable(policy):
    invoice = inv("INV-1", 100000)
    got = match(pay(116000, narration="INV-1"), [invoice], customer(), policy.tds)
    assert got.outcome == "reference"
    assert not g_pay(got.candidates[0], open_balance(invoice)).allowed
    assert g_pay(got.candidates[0], open_balance(invoice), tds_evidenced_paise=2000 * R).allowed


def test_overpayment_has_no_candidate(policy):
    assert match(pay(999999), SAHYADRI[:2], customer(), policy.tds).outcome == "none"


def test_other_customers_and_settled_invoices_are_ignored(policy):
    invoices = [inv("INV-1", 100000, customer="C2"), inv("INV-2", 100000)]
    got = match(pay(118000), invoices, customer(rate=0), policy.tds, paid={"INV-2": 118000 * R})
    assert got.outcome == "none"


@pytest.mark.parametrize(
    "narration,expected",
    [("NEFT/INV-882/ABC", {"INV-882"}), ("inv 882 part", {"INV-882"}), ("INV8820", set()), ("PINV-882", set()),
     ("NEFT-XYZ 882", set())],
)
def test_invoice_citation(narration, expected):
    assert cited_numbers(narration, [inv("INV-882", 1000)]) == expected


def test_payer_resolution():
    who = customer()
    assert resolve_payer(pay(1), [who]) == who
    assert resolve_payer(pay(1, payer="KONKAN MARINE"), [who]) is None


def test_fmt_inr():
    assert [fmt_inr(v) for v in (444860 * R, 100 * R, 767050, 12345678900, -5000 * R)] == [
        "₹4,44,860", "₹100", "₹7,670.50", "₹12,34,56,789", "-₹5,000",
    ]
