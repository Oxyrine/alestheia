"""Three independent confidence gates. Pure functions."""
from dlra.models import Allocation, Contact, Diagnosis, Evidence, EvidenceStatus, Fallback, Verdict


def _norm(email: str) -> str:
    return email.strip().casefold()


def resolve_contacts(
    contacts: list[Contact], replied_from: set[str] = frozenset(), human_confirmed: set[str] = frozenset()
) -> list[Contact]:
    """Derive each contact's status. Only this function should mark a contact verified.

    verified: on the ledger and the buyer has replied from it, or a human confirmed it.
    conflicting: another source gives a different address, until a human picks one.
    """
    replied = {_norm(e) for e in replied_from}
    confirmed = {_norm(e) for e in human_confirmed}

    def status(c: Contact) -> EvidenceStatus:
        email = _norm(c.email)
        if email in confirmed:
            return EvidenceStatus.VERIFIED
        if confirmed:  # a human picked another address
            return EvidenceStatus.UNVERIFIED
        if any(
            o.customer_id == c.customer_id and o.source != c.source and _norm(o.email) != email
            # an address the same source also lists is agreement, not a conflict
            and not any(_norm(s.email) == _norm(o.email) and s.source == c.source for s in contacts)
            and not any(_norm(s.email) == email and s.source == o.source for s in contacts)
            for o in contacts
        ):
            return EvidenceStatus.CONFLICTING
        if c.source == "ledger" and email in replied:
            return EvidenceStatus.VERIFIED
        return EvidenceStatus.UNVERIFIED

    return [c.model_copy(update={"status": status(c)}) for c in contacts]


def _fail(rule_id: str, reason: str, evidence_ids: list[str] = (), fallback=Fallback.CARD) -> Verdict:
    return Verdict(
        allowed=False, rule_id=rule_id, reason=reason, fallback=fallback, evidence_ids=list(evidence_ids)
    )


def g_contact(target: Contact | None, contacts: list[Contact] = ()) -> Verdict:
    if target is None:
        return _fail("G-CONTACT", "No contact on file for this buyer.")
    clashing = [c for c in contacts if c.status == EvidenceStatus.CONFLICTING and c.customer_id == target.customer_id]
    ids = [c.source_evidence_id for c in (target, *clashing) if c.source_evidence_id]
    ids = list(dict.fromkeys(ids))
    if target.opted_out:
        return _fail("G-CONTACT", f"{target.email} has opted out.", ids)
    if target.status != EvidenceStatus.VERIFIED:
        return _fail("G-CONTACT", f"{target.email} is {target.status}, not verified.", ids)
    if clashing:
        others = ", ".join(sorted({c.email for c in clashing}))
        return _fail("G-CONTACT", f"Conflicting contact for this buyer is unresolved: {others}.", ids)
    return Verdict(allowed=True, rule_id="G-CONTACT", evidence_ids=ids)


def g_diag(diagnosis: Diagnosis, evidence: list[Evidence], threshold: float) -> Verdict:
    conflicting = [
        e.id for e in evidence if e.id in diagnosis.evidence_ids and e.status == EvidenceStatus.CONFLICTING
    ]
    if conflicting:
        return _fail("G-DIAG", "Key evidence is conflicting.", conflicting)
    if diagnosis.confidence < threshold:
        return _fail(
            "G-DIAG",
            f"Diagnosis confidence {diagnosis.confidence:.2f} is below {threshold:.2f}.",
            diagnosis.evidence_ids,
        )
    return Verdict(allowed=True, rule_id="G-DIAG", evidence_ids=diagnosis.evidence_ids)


def g_pay(
    allocation: Allocation | None,
    open_balance_paise: int,
    *,
    tds_evidenced_paise: int = 0,
    threshold: float = 0.95,
    tolerance_paise: int = 100,
) -> Verdict:
    """`open_balance_paise` is the open balance of exactly the invoices the allocation covers."""
    if allocation is None:
        return _fail("G-PAY", "No allocation to post.")
    why = []
    if allocation.confidence < threshold:
        why.append(f"match confidence {allocation.confidence:.2f} is below {threshold:.2f}")
    if not allocation.cites_invoice:
        why.append("no invoice reference")
    if tds_evidenced_paise:
        slack = tolerance_paise * len(allocation.lines)
        exact = abs(allocation.amount_paise + tds_evidenced_paise - open_balance_paise) <= slack
    else:
        exact = allocation.amount_paise == open_balance_paise
    if not exact:
        why.append("amount does not equal the open balance and the gap is not evidenced")
    if why:
        return _fail("G-PAY", "Suggest only: " + "; ".join(why) + ".")
    return Verdict(allowed=True, rule_id="G-PAY")
