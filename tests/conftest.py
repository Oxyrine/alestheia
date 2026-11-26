from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from dlra.models import Contact, Diagnosis, Evidence
from dlra.policy import PolicyContext, load_policy

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=IST)  # a Friday, in business hours


@pytest.fixture
def policy():
    return load_policy()


def contact(email="accounts@buyer.example", status="verified", **kw):
    return Contact(id=kw.pop("id", email), customer_id="C1", email=email, status=status, **kw)


def evidence(id="E1", source_type="email", status="verified", **kw):
    return Evidence(
        id=id, case_id="K1", source_type=source_type, source_ref="ref", ts=NOW,
        field="f", claim="c", status=status, **kw,
    )


@pytest.fixture
def make_ctx(policy):
    """A context where every gate passes, so a test sees only the rule it is aiming at."""

    def build(state, confidence=0.85, **kw):
        target = kw.pop("contact", contact())
        base = dict(
            state=state, now=NOW, policy=policy,
            diagnosis=Diagnosis(state=state, confidence=confidence, evidence_ids=["E1"]),
            contact=target, contacts=[target] if target else [], evidence=[evidence()],
        )
        return PolicyContext(**(base | kw))

    return build
