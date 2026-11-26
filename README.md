# Diagnosis-Locked Receivables Agent

Finds out why an invoice is unpaid, fixes what it can, and stops when the evidence is thin.

**The model may interpret language, but it cannot decide to break a rule.**

Work in progress. All data is synthetic.

```
uv sync
uv run pytest -q
```

## Built so far: the engine

| Module | What it does |
| --- | --- |
| `dlra/rules.py` | The rule table (allowed and forbidden per state) and legal state transitions |
| `dlra/gates.py` | Contact identity, diagnosis and payment-match gates |
| `dlra/policy.py` | `authorise`: a pure function that permits or refuses every action, with a rule ID |
| `dlra/reconcile.py` | Matches payments to invoices, including TDS short-payments; proposes, never posts |
| `dlra/promises.py` | Turns a buyer's reply into a promise; relative dates stay unresolved |
| `dlra/events.py` | Append-only, deduplicated event log with a loop guard |
| `dlra/engine.py` | Diagnosis in; permitted actions, refusals and human cards out |

The diagnosis, extraction and drafting layer is not built yet. Tests supply the diagnosis directly.

Where the conduct rules would block a reply that resolves the thing that caused them, the engine allows
only that reply: a bank-reference request after a reported payment, and dispute messages during a dispute.
Opt-out and a human pause block everything.
