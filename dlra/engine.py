"""The run loop.

`decide` is pure: bundle + diagnosis in, everything that should happen out. The demo and the
eval both go through it. `run_case` adds single-flight and persistence. Code picks the actions;
the LLM never does. Every action passes through policy.authorise.
"""
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dlra import events, gates, promises, reconcile
from dlra.db import new_id, transaction, utc_iso
from dlra.models import Action as A
from dlra.models import (
    Allocation,
    CaseBundle,
    Contact,
    Diagnosis,
    EvidenceStatus,
    Fallback,
    Outcome,
    Promise,
    PromiseStatus,
    State,
    Verdict,
    fmt_inr,
)
from dlra.policy import Policy, PolicyContext, ProposedAction, authorise
from dlra.rules import OUTBOUND, check_transition, rule_text

# (intent, bundle, facts) -> (subject, body, facts_text). Lane B's draft.py plugs in here.
Drafter = Callable[[str, CaseBundle, dict], tuple[str, str, str]]


def stub_draft(intent: str, bundle: CaseBundle, facts: dict) -> tuple[str, str, str]:
    """Placeholder until draft.py lands. Neutral text only."""
    number = bundle.invoice.number
    return f"Regarding invoice {number}", f"[{intent}] A note about invoice {number}.", ""


# What age-based chasing would do. Proposed only so the refusal is on the record; never executed.
PROBES = {
    State.PAPERWORK: (A.ESCALATION,),
    State.DISPUTE: (A.PAYMENT_REMINDER,),
    State.CASH_CONSTRAINT: (A.INCREASE_CONTACT,),
    State.PROMISE_ACTIVE: (A.ESCALATION,),
    State.PROMISE_BROKEN: (A.HARSHER_MESSAGE,),
    State.PAID_UNMATCHED: (A.PAYMENT_REMINDER,),
    State.SILENT_UNKNOWN: (A.ESCALATION,),
}


@dataclass
class Card:
    kind: str
    reason: str
    rule_id: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


@dataclass
class Decision:
    action: A
    verdict: Verdict
    outcome: Outcome
    probe: bool = False
    card_kind: str | None = None


@dataclass
class Message:
    action: A
    intent: str
    contact_id: str
    subject: str
    body: str
    facts_text: str = ""
    status: str = "queued"


@dataclass
class RunResult:
    state: State | None
    diagnosis: Diagnosis
    under_review: bool = False
    safe_pause_until: datetime | None = None
    decisions: list[Decision] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    block_queued: Verdict | None = None  # reminders already queued must not go out
    promise: Promise | None = None
    match: reconcile.MatchResult | None = None
    payment_id: str | None = None
    confirmed: Allocation | None = None
    gates: dict = field(default_factory=dict)

    def refusals(self) -> list[Decision]:
        return [d for d in self.decisions if not d.verdict.allowed and d.verdict.fallback != Fallback.QUEUE]

    def card(self, kind: str) -> Card | None:
        return next((c for c in self.cards if c.kind == kind), None)


def pick_contact(contacts: list[Contact]) -> Contact | None:
    """The ledger master is who we would write to. The gates decide whether we may."""
    return min(contacts, key=lambda c: (c.source != "ledger", c.status != "verified"), default=None)


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].casefold()


def _card_kind(rule_id: str) -> str:
    if rule_id in ("H-02", "G-CONTACT"):
        return "low_conf_contact"
    if rule_id.startswith("L-"):
        return "out_of_policy_request"
    if rule_id in ("H-04", "C-CAP"):
        return "contact_blocked"
    if rule_id in ("H-01", "H-07"):
        return "formal_notice_draft"
    return "low_conf_diagnosis"


class _Run:
    def __init__(self, bundle: CaseBundle, diagnosis: Diagnosis, policy: Policy, now: datetime, draft: Drafter):
        self.b, self.d, self.policy, self.now, self.draft = bundle, diagnosis, policy, now, draft
        self.today = now.astimezone(ZoneInfo(policy.business_hours.tz)).date()
        self.facts = bundle.facts
        self.contact = pick_contact(bundle.contacts)
        self.result = RunResult(state=diagnosis.state, diagnosis=diagnosis, promise=bundle.promise)

    def context(self, **extra) -> PolicyContext:
        f, promise = self.facts, self.result.promise
        paid = f.confirmed_paid.get(self.b.invoice.id, 0)
        queued = [m.action for m in self.result.messages]
        base = dict(
            state=self.result.state, now=self.now, policy=self.policy, diagnosis=self.d,
            contact=self.contact, contacts=self.b.contacts, evidence=self.b.evidence,
            payment_reported=f.payment_reported, dispute_open=f.dispute_open, human_pause=f.human_pause,
            # messages queued earlier in this run count against the weekly cap
            sent_this_week=f.sent_this_week + len(queued),
            neutral_questions_sent=f.neutral_questions_sent + queued.count(A.NEUTRAL_QUESTION_ONCE),
            payment_report_expired=f.payment_report_expired,
            bank_ref_asks_since_expiry=f.bank_ref_asks_since_expiry + queued.count(A.ASK_BANK_REF),
            promise=promise,
            next_due=promises.next_due(promise, paid) if promise and promise.status == PromiseStatus.ACTIVE else None,
        )
        return PolicyContext(**(base | extra))

    def card(self, kind: str, reason: str, rule_id: str | None = None, **payload) -> Card:
        if existing := self.result.card(kind):
            return existing
        card = Card(kind, reason, rule_id, list(self.d.evidence_ids), payload)
        self.result.cards.append(card)
        return card

    def record(self, action: A, verdict: Verdict, outcome: Outcome, **kw) -> Decision:
        decision = Decision(action, verdict, outcome, **kw)
        self.result.decisions.append(decision)
        return decision

    def attempt(
        self, action: A, *, intent: str | None = None, outcome: Outcome = Outcome.ACT, card: str | None = None,
        card_payload: dict | None = None, card_reason: str | None = None, draft_facts: dict | None = None,
        context: dict | None = None, **proposal,
    ) -> bool:
        """Authorise, then execute or run the rule's fallback. Returns True if permitted."""
        if intent and intent in self.facts.sent_intents:
            return False  # already queued or sent: a re-run must not repeat it
        ctx = self.context(**(context or {}))
        proposed = ProposedAction(action=action, **proposal)
        verdict = authorise(ctx, proposed)
        message = None
        if action in OUTBOUND and intent and (verdict.allowed or verdict.fallback == Fallback.QUEUE):
            subject, body, facts_text = self.draft(intent, self.b, draft_facts or {})
            proposed = proposed.model_copy(update={"text": f"{subject}\n{body}", "facts_text": facts_text})
            verdict = authorise(ctx, proposed)  # again, so tone and leak lints see the text
            message = Message(action, intent, self.contact.id if self.contact else "", subject, body, facts_text)

        if verdict.allowed or verdict.fallback == Fallback.QUEUE:
            kind = self.card(card, card_reason or verdict.reason, verdict.rule_id, **(card_payload or {})).kind \
                if card else None
            if message:
                self.result.messages.append(message)
            self.record(action, verdict, Outcome.CARD if card and not message else outcome, card_kind=kind)
            return True

        if verdict.fallback == Fallback.SAFE_PAUSE:
            self.pause()
        # a refusal is only shown with a card: attach to this run's card, or raise one
        wanted = _card_kind(verdict.rule_id)
        existing = self.result.card(wanted) or (self.result.cards[-1] if self.result.cards else None)
        kind = existing.kind if existing else self.card(wanted, verdict.reason, verdict.rule_id).kind
        self.record(action, verdict, Outcome.CARD, card_kind=kind)
        return False

    def pause(self) -> None:
        self.result.safe_pause_until = self.now + timedelta(days=self.policy.safe_pause_days)

    def probe(self) -> None:
        # A refusal is only ever shown together with a card, so probes need one to attach to.
        if not self.result.cards:
            return
        for action in PROBES.get(self.result.state, ()):
            verdict = authorise(self.context(), ProposedAction(action=action))
            if not verdict.allowed:
                self.record(action, verdict, Outcome.CARD, probe=True, card_kind=self.result.cards[0].kind)

    # --- one planner per state -------------------------------------------------

    def paperwork(self) -> None:
        if self.facts.correction_on_file:
            self.attempt(
                A.DRAFT_CORRECTION, card="material_correction",
                card_reason="The corrected document changes what the buyer receives: approve before it is reissued.",
            )
        else:
            self.attempt(A.REQUEST_ONE_ITEM, intent="request_missing_item")

    def dispute(self) -> None:
        self.attempt(
            A.STOP_REMINDERS, card="dispute_dossier", card_reason="Buyer disputes this invoice. Dossier attached.",
        )
        self.result.block_queued = authorise(
            self.context(dispute_open=True), ProposedAction(action=A.PAYMENT_REMINDER)
        )
        self.attempt(A.DISPUTE_MESSAGE, intent="dispute_ack_request_evidence")

    def cash_constraint(self) -> None:
        promise = self.result.promise
        if promise is None and self.facts.promise_extract:
            invoice = self.b.invoice
            try:
                promise = promises.build_promise(
                    self.facts.promise_extract,
                    reconcile.open_balance(invoice, self.facts.confirmed_paid),
                    self.today,
                    f"PRM-{self.b.case_id}",
                )
            except (ValueError, KeyError, TypeError) as error:
                self.attempt(A.CARD_SUPPLIER, card="low_conf_diagnosis",
                             card_reason=f"The buyer's payment plan could not be read: {error}.")
                return
        if promise is None:
            self.attempt(A.CARD_SUPPLIER, card="low_conf_diagnosis",
                         card_reason="The buyer is short of cash and has not proposed a plan.")
            return
        self.result.promise = promise
        # the plan exactly as asked, so an out-of-policy condition is refused on the record
        self.attempt(A.PROPOSE_PLAN, outcome=Outcome.PROPOSE, plan=promise)
        if promise.concessions:
            self.attempt(A.DECLINE_CONDITION, intent="decline_condition_neutral")
        if promise.status in (PromiseStatus.PROPOSED, PromiseStatus.AWAITING_CONFIRMATION):
            asked = self.attempt(
                A.CONFIRMATION_REQUEST, intent="confirmation_request_exact_dates",
                draft_facts={"installments_paise": [i.amount_paise for i in promise.installments]},
            )
            if asked:
                self.result.promise = promises.request_confirmation(promise)

    def promise_active(self) -> None:
        self.attempt(A.TRACK_PROMISE)
        context = self.context()
        if context.next_due and 0 <= (context.next_due - self.today).days <= self.policy.reminder_window_days:
            self.attempt(A.REMINDER_NEAR_DUE, intent=f"reminder_near_due:{context.next_due}")

    def promise_broken(self) -> None:
        self.attempt(A.REDIAGNOSE)
        self.attempt(A.CARD_SUPPLIER, card="promise_broken", card_reason="A promised instalment was missed.")

    def paid_unmatched(self) -> None:
        payments = [p for p in self.b.payments if p.id not in self.facts.allocated_payment_ids]
        if not payments:
            self.attempt(A.ASK_BANK_REF, intent="ask_bank_reference")
            self.pause()
            return
        payment = max(payments, key=lambda p: (p.value_date, p.id))
        f = self.facts
        match = reconcile.match(
            payment, self.b.open_invoices or [self.b.invoice], self.b.customer, self.policy.tds,
            paid=f.confirmed_paid, reported_tds_paise=f.reported_tds_paise,
            reported_invoice_numbers=set(f.reported_invoice_numbers),
            remittance_invoice_numbers=set(f.remittance_invoice_numbers),
        )
        self.result.match, self.result.payment_id = match, payment.id
        if not match.candidates:
            self.attempt(A.CARD_AMBIGUOUS_MATCH, card="ambiguous_allocation", card_reason=match.reason,
                         card_payload={"payment_id": payment.id, "candidates": []})
            self.attempt(A.ASK_BANK_REF, intent="ask_bank_reference")
            return

        best = match.candidates[0]
        by_id = {i.id: i for i in (self.b.open_invoices or [self.b.invoice])}
        balance = sum(reconcile.open_balance(by_id[i], f.confirmed_paid) for i in best.lines)
        # TDS evidence is held per case, so it only counts when the match is this invoice alone
        evidenced = f.tds_evidenced_paise if set(best.lines) == {self.b.invoice.id} else 0
        posting = dict(allocation=best, open_balance_paise=balance, tds_evidenced_paise=evidenced)
        post = authorise(self.context(**posting), ProposedAction(action=A.MARK_PAID))
        if post.allowed:  # G-PAY passed, which needs an exact, referenced match
            self.result.confirmed = best
            self.record(A.MARK_PAID, post, Outcome.ACT)
            return

        self.attempt(A.SUGGEST_ALLOCATION, outcome=Outcome.PROPOSE)
        why = _not_posted_reason(best, balance, evidenced)
        self.attempt(
            A.CARD_AMBIGUOUS_MATCH, card="ambiguous_allocation", card_reason=why,
            card_payload={
                "payment_id": payment.id, "outcome": match.outcome, "favoured_because": match.reason,
                "lean": match.lean, "candidates": [c.model_dump(mode="json") for c in match.candidates],
            },
        )
        self.record(A.MARK_PAID, post.model_copy(update={"reason": why}), Outcome.CARD, probe=True,
                    card_kind="ambiguous_allocation")
        self.attempt(A.ASK_BANK_REF, intent="ask_bank_reference",
                     draft_facts={"amount": fmt_inr(payment.amount_paise)})

    def silent_unknown(self) -> None:
        contact = gates.g_contact(self.contact, self.b.contacts)
        if not contact.allowed:
            self.contact_card(contact)
        self.attempt(A.NEUTRAL_QUESTION_ONCE, intent="neutral_question")
        self.attempt(A.SAFE_PAUSE, outcome=Outcome.ABSTAIN)
        self.pause()

    def resolved(self) -> None:
        self.attempt(A.STOP_OUTREACH)
        self.attempt(A.ARCHIVE)

    def contact_card(self, verdict: Verdict) -> None:
        emails = sorted({c.email for c in self.b.contacts})
        clash = any(c.status == EvidenceStatus.CONFLICTING for c in self.b.contacts)
        payload = {"addresses": [f"{c.email} ({c.source}, {c.status})" for c in self.b.contacts]}
        reason = verdict.reason
        if clash and len({_domain(e) for e in emails}) > 1:
            payload["sub_tag"] = "possible_redirection_fraud"
            reason += (" The addresses are on different domains, a payment-redirection fraud pattern. "
                       "Verify by phone with the contact you already know.")
        card = self.card("low_conf_contact", reason, verdict.rule_id, **payload)
        card.evidence_ids = list(dict.fromkeys(
            [*verdict.evidence_ids, *(c.source_evidence_id for c in self.b.contacts if c.source_evidence_id)]
        ))


def _not_posted_reason(best: Allocation, balance: int, evidenced: int) -> str:
    why = []
    if not best.cites_invoice:
        why.append("no invoice reference (G-PAY)")
    if best.amount_paise + evidenced < balance and best.tds_paise and not evidenced:
        why.append("TDS not evidenced (H-03)")
    if not why:
        why.append("the match is not certain enough (G-PAY)")
    return f"Likely, but not proven: {' and '.join(why)}."


def decide(
    bundle: CaseBundle, diagnosis: Diagnosis, policy: Policy, now: datetime, draft: Drafter = stub_draft
) -> RunResult:
    run = _Run(bundle, diagnosis, policy, now, draft)
    result, facts = run.result, bundle.facts
    invoice = bundle.invoice

    # A diagnosis may only move the case along a legal transition.
    view = reconcile.settlement(
        invoice.gross_paise - invoice.credit_note_paise, facts.confirmed_paid.get(invoice.id, 0),
        facts.reported_tds_paise or 0, facts.tds_evidenced_paise,
    )
    ok, rule_id, reason = check_transition(
        bundle.state, diagnosis.state,
        human_reopen=facts.human_reopen, settled=view.status == "settled",
        promise_active=bool(bundle.promise and bundle.promise.status == PromiseStatus.ACTIVE),
        deadline_missed=facts.deadline_missed,
    )
    if not ok:
        result.state, result.under_review = bundle.state, True
        run.card("low_conf_diagnosis", reason, rule_id, proposed_state=str(diagnosis.state))
        return result

    diag = gates.g_diag(diagnosis, bundle.evidence, policy.diag_threshold)
    result.gates = {
        "G-CONTACT": gates.g_contact(run.contact, bundle.contacts).model_dump(mode="json"),
        "G-DIAG": diag.model_dump(mode="json"),
    }
    distressed = facts.distress_signal and bundle.customer.payment_history.get("on_time_ratio", 0) >= 0.8
    if distressed:
        result.under_review = True
        run.card("distress_review",
                 "Public distress signal against a good payment history. It proves nothing about this "
                 "invoice (H-05): review before any contact beyond one neutral question.",
                 "R-DISTRESS-REVIEW")
    elif not diag.allowed:
        result.under_review = True
        run.card("low_conf_diagnosis", diag.reason, "G-DIAG", proposed_state=str(diagnosis.state))

    if facts.payment_report_expired:
        run.card("reported_payment_not_found",
                 "The buyer reported a payment that has not shown up in the bank or a webhook.",
                 "R-PAYMENT-REPORT-EXPIRED")
        run.attempt(A.ASK_BANK_REF, intent="ask_bank_reference_after_expiry")

    getattr(run, result.state.lower())()

    if result.confirmed:
        paid = facts.confirmed_paid.get(invoice.id, 0) + result.confirmed.lines.get(invoice.id, 0)
        if reconcile.settlement(
            invoice.gross_paise - invoice.credit_note_paise, paid,
            facts.reported_tds_paise or 0, facts.tds_evidenced_paise,
        ).status == "settled":
            result.state = State.RESOLVED
            run.resolved()

    run.probe()
    return result


# --- persistence -------------------------------------------------------------


def run_case(
    conn: sqlite3.Connection,
    case_id: str,
    trigger_event_id: str,
    load_bundle: Callable[[sqlite3.Connection, str], CaseBundle],
    propose: Callable[[CaseBundle], Diagnosis],
    policy: Policy,
    now: datetime,
    draft: Drafter = stub_draft,
) -> list[RunResult]:
    """Single-flight per case. Events arriving mid-run are coalesced into one more run."""
    claimed = conn.execute("UPDATE cases SET running = 1 WHERE id = ? AND running = 0", (case_id,)).rowcount
    if not claimed:
        conn.execute("UPDATE cases SET rerun_pending = 1 WHERE id = ?", (case_id,))
        return []
    results = []
    try:
        while True:
            bundle = load_bundle(conn, case_id)
            result = decide(bundle, propose(bundle), policy, now, draft)
            persist(conn, case_id, trigger_event_id, result, now)
            results.append(result)
            released = conn.execute(
                "UPDATE cases SET running = 0 WHERE id = ? AND rerun_pending = 0", (case_id,)
            ).rowcount
            if released:
                return results
            conn.execute("UPDATE cases SET rerun_pending = 0 WHERE id = ?", (case_id,))
    except BaseException:
        conn.execute("UPDATE cases SET running = 0 WHERE id = ?", (case_id,))
        raise


def _open_card(conn: sqlite3.Connection, case_id: str, card: Card) -> str:
    row = conn.execute(
        "SELECT id FROM cards WHERE case_id = ? AND kind = ? AND status = 'open'", (case_id, card.kind)
    ).fetchone()
    if row:
        return row["id"]
    card_id = new_id("CARD")
    conn.execute(
        "INSERT INTO cards (id, case_id, kind, reason, rule_id, evidence_ids, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_id, case_id, card.kind, card.reason, card.rule_id, json.dumps(card.evidence_ids),
         json.dumps(card.payload)),
    )
    return card_id


def persist(conn: sqlite3.Connection, case_id: str, trigger_event_id: str, result: RunResult, now: datetime) -> str:
    run_id = new_id("RUN")
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs (id, case_id, trigger_event_id, started_at, finished_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'done')",
            (run_id, case_id, trigger_event_id, utc_iso(now), utc_iso()),
        )
        card_ids = {card.kind: _open_card(conn, case_id, card) for card in result.cards}

        for decision in result.decisions:
            decision_id = new_id("DEC")
            verdict = decision.verdict
            conn.execute(
                "INSERT INTO decisions (id, run_id, case_id, state, confidence, action, outcome, allowed, probe, "
                "rule_id, reason, evidence_ids, gates, card_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_id, run_id, case_id, result.state, result.diagnosis.confidence, decision.action,
                 decision.outcome, int(verdict.allowed), int(decision.probe), verdict.rule_id, verdict.reason,
                 json.dumps(verdict.evidence_ids), json.dumps(result.gates), card_ids.get(decision.card_kind)),
            )
            for evidence_id in verdict.evidence_ids:
                conn.execute(
                    "UPDATE evidence SET influenced_decision_id = ? WHERE id = ? AND influenced_decision_id IS NULL",
                    (decision_id, evidence_id),
                )

        if block := result.block_queued:
            conn.execute(
                "UPDATE messages SET status = 'blocked', rule_id = ? "
                "WHERE case_id = ? AND status = 'queued' AND action IN (?, ?)",
                (block.rule_id, case_id, A.PAYMENT_REMINDER, A.REMINDER_NEAR_DUE),
            )
        for message in result.messages:
            conn.execute(
                "INSERT INTO messages (id, case_id, contact_id, action, intent, subject, body, facts_text, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id("MSG"), case_id, message.contact_id, message.action, message.intent, message.subject,
                 message.body, message.facts_text, message.status),
            )
        if promise := result.promise:
            conn.execute(
                "INSERT INTO promises (id, case_id, data, status) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET data = excluded.data, status = excluded.status",
                (promise.id, case_id, promise.model_dump_json(), promise.status),
            )

        if result.payment_id:  # a re-run replaces earlier suggestions; confirmed rows are never touched
            conn.execute(
                "DELETE FROM allocations WHERE payment_id = ? AND status = 'proposed'", (result.payment_id,)
            )
        candidates = [result.confirmed] if result.confirmed else (result.match.candidates if result.match else [])
        for rank, candidate in enumerate(candidates):
            for invoice_id, amount in candidate.lines.items():
                conn.execute(
                    "INSERT INTO allocations (id, payment_id, invoice_id, candidate, amount_paise, confidence, "
                    "basis, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (payment_id, invoice_id, candidate) DO NOTHING",
                    (new_id("ALC"), result.payment_id, invoice_id, rank, amount, candidate.confidence,
                     json.dumps(candidate.basis), "confirmed" if result.confirmed else "proposed"),
                )

        conn.execute(
            "UPDATE cases SET state = ?, diagnosis_confidence = ?, under_review = ?, safe_pause_until = ?, "
            "updated_at = ? WHERE id = ?",
            (result.state, result.diagnosis.confidence, int(result.under_review),
             utc_iso(result.safe_pause_until) if result.safe_pause_until else None, utc_iso(now), case_id),
        )
        events.append(
            conn, "run_completed", "engine", {"run_id": run_id}, f"run:{run_id}", case_id, produced_by_agent=True
        )
    return run_id


def describe(result: RunResult) -> str:
    """Human-readable run summary for the CLI."""
    lines = [
        f"state={result.state} confidence={result.diagnosis.confidence:.2f}"
        + (" under_review" if result.under_review else "")
    ]
    for d in result.decisions:
        if d.verdict.fallback == Fallback.QUEUE:
            lines.append(f"  queued   {d.action} (outside business hours)")
        elif d.verdict.allowed:
            lines.append(f"  {d.outcome.lower():8} {d.action} [{d.verdict.rule_id}]")
        else:
            reason = d.verdict.reason or rule_text(d.verdict.rule_id)
            card = f" -> card {d.card_kind}" if d.card_kind else ""
            lines.append(f"  REFUSED  {d.action} [{d.verdict.rule_id}] {reason}{card}")
    lines += [f"  message  {m.intent}" for m in result.messages]
    lines += [f"  card     {c.kind}: {c.reason}" for c in result.cards]
    if result.promise:
        lines.append(f"  promise  {json.dumps(promises.spec_json(result.promise))}")
    if result.match:
        lines.append(f"  match    {result.match.outcome}: {result.match.reason} {result.match.lean or ''}".rstrip())
        lines += [f"           {sorted(c.lines)} {c.kind} tds={fmt_inr(c.tds_paise)}" for c in result.match.candidates]
    return "\n".join(lines)

