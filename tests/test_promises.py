from datetime import date

import pytest

from dlra.promises import (
    build_promise,
    confirm,
    map_condition,
    next_due,
    on_deadline,
    parse_due,
    request_confirmation,
    spec_json,
)

TODAY = date(2026, 11, 27)
CASE_3 = {
    "invoice_ids": ["INV-882"],
    "installments": [{"share": 0.5, "due_text": "next week"}, {"due_text": "after month end"}],
    "conditions": ["waive late fee"],
}


def case3():
    return build_promise(CASE_3, 420000_00, TODAY, "PRM-221", "E1")


def test_case_3_matches_the_spec_json():
    assert spec_json(case3()) == {
        "promise_id": "PRM-221",
        "invoice_ids": ["INV-882"],
        "installments": [
            {"amount": 210000, "due": "next week", "date_status": "unresolved"},
            {"amount": 210000, "due": "after month end", "date_status": "unresolved"},
        ],
        "conditions": ["waive late fee"],
        "status": "proposed",
    }
    assert case3().concessions == ["late_fee_waiver"]


def test_rounding_sums_exactly():
    thirds = {"invoice_ids": ["I"], "installments": [
        {"share": 1 / 3, "due_text": "x"}, {"share": 1 / 3, "due_text": "y"}, {"share": 1 / 3, "due_text": "z"}]}
    got = build_promise(thirds, 100000_00, TODAY, "P")
    assert got.total_paise == 100000_00
    assert [i.amount_paise for i in got.installments] == [33333_00, 33333_00, 33334_00]


@pytest.mark.parametrize("amounts", [[500_00, None], [None, None]])
def test_impossible_amounts_are_rejected(amounts):
    bad = {"invoice_ids": ["I"], "installments": [{"amount_paise": a, "due_text": "x"} for a in amounts]}
    with pytest.raises(ValueError):
        build_promise(bad, 100_00, TODAY, "P")


@pytest.mark.parametrize(
    "text", ["next week", "after month end", "after 15 Dec", "soon", "15", "December", "31 Feb 2027",
             "15 Dec 2025", "mid December", "Friday"],
)
def test_relative_or_bad_dates_stay_unresolved(text):
    assert parse_due(text, TODAY) is None


@pytest.mark.parametrize(
    "text,expected",
    [("15 Dec", date(2026, 12, 15)), ("by 15th December 2026", date(2026, 12, 15)), ("2026-12-15", date(2026, 12, 15)),
     ("15/12/2026", date(2026, 12, 15)), ("15-Dec-26", date(2026, 12, 15)), ("Dec 15", date(2026, 12, 15)),
     ("5 Jan", date(2027, 1, 5))],
)
def test_absolute_dates_resolve(text, expected):
    assert parse_due(text, TODAY) == expected


def test_conditions_fail_closed():
    assert map_condition("drop the late fee") == ["late_fee_waiver"]
    assert map_condition("give us free freight") == ["unknown_condition"]
    assert map_condition("drop the late fee", llm_code="nothing_to_see") == ["late_fee_waiver"]


def test_active_only_after_confirmation_and_policy(policy):
    with pytest.raises(ValueError):
        confirm(case3(), ["5 Dec", "5 Jan"], policy.limits, TODAY)
    waiting = request_confirmation(case3())

    still_vague, why = confirm(waiting, ["5 Dec", "after month end"], policy.limits, TODAY)
    assert still_vague.status == "awaiting_confirmation" and why[0][0] == "L-TENURE"

    with_waiver, why = confirm(waiting, ["5 Dec", "5 Jan"], policy.limits, TODAY, conditions=["waive the late fee"])
    assert (with_waiver.status, with_waiver.policy_ok, why[0][0]) == ("awaiting_confirmation", False, "L-CONCESSION")
    overridden, why = confirm(waiting, ["5 Dec", "5 Jan"], policy.limits, TODAY,
                              conditions=["waive the late fee"], approved_concessions={"late_fee_waiver"})
    assert overridden.status == "active" and not why

    active, why = confirm(waiting, ["5 Dec", "5 Jan"], policy.limits, TODAY)
    assert (active.status, active.buyer_confirmed, active.policy_ok, why) == ("active", True, True, [])


def test_deadlines(policy):
    active, _ = confirm(request_confirmation(case3()), ["5 Dec", "5 Jan"], policy.limits, TODAY)
    assert on_deadline(active, 0, date(2026, 12, 7), 2).status == "active"      # inside grace
    assert on_deadline(active, 0, date(2026, 12, 8), 2).status == "broken"
    assert on_deadline(active, 210000_00, date(2026, 12, 8), 2).status == "active"
    assert next_due(active, 210000_00) == date(2027, 1, 5)
    assert on_deadline(active, 420000_00, date(2027, 1, 9), 2).status == "fulfilled"
    assert on_deadline(active, 210000_00, date(2027, 1, 9), 2).status == "broken"
