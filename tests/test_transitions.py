import pytest

from dlra.models import State as S
from dlra.rules import check_transition


def test_resolved_is_terminal_unless_a_human_reopens():
    assert check_transition(S.RESOLVED, S.DISPUTE)[1] == "T-RESOLVED-TERMINAL"
    assert check_transition(S.RESOLVED, S.DISPUTE, human_reopen=True)[0]


@pytest.mark.parametrize("prev", [None, S.PAID_UNMATCHED, S.DISPUTE])
def test_resolved_needs_a_settled_invoice(prev):
    assert check_transition(prev, S.RESOLVED)[1] == "T-RESOLVED-SETTLED"
    assert check_transition(prev, S.RESOLVED, settled=True)[0]


def test_promise_broken_only_from_active_on_a_missed_deadline():
    assert not check_transition(S.CASH_CONSTRAINT, S.PROMISE_BROKEN, deadline_missed=True)[0]
    assert not check_transition(S.PROMISE_ACTIVE, S.PROMISE_BROKEN)[0]
    assert check_transition(S.PROMISE_ACTIVE, S.PROMISE_BROKEN, deadline_missed=True)[0]


def test_promise_active_needs_an_active_promise():
    assert not check_transition(S.CASH_CONSTRAINT, S.PROMISE_ACTIVE)[0]
    assert check_transition(S.CASH_CONSTRAINT, S.PROMISE_ACTIVE, promise_active=True)[0]


def test_ordinary_rediagnosis_is_free():
    assert check_transition(S.PAID_UNMATCHED, S.SILENT_UNKNOWN)[0]
    assert check_transition(S.PAPERWORK, S.DISPUTE)[0]
