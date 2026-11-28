# Diagnosis-Locked Receivables Agent

Finds out why an invoice is unpaid, fixes what it can, and stops when the evidence is thin.

**The model may interpret language, but it cannot decide to break a rule.**

Work in progress. All data is synthetic.

```
uv sync
uv run pytest -q
uv run python -m scripts.replay_demo     # the six demo cases; nothing is emailed
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
| `dlra/loader.py` | Builds a case from the database; code derives every flag the engine acts on |
| `dlra/service.py` | Ingestion, deadline effects, human decisions, and waking the engine |
| `dlra/outbox.py` | Re-authorises every message at send time |
| `dlra/scheduler.py` | Emits deadline events; it has no way to send mail itself |
| `dlra/mailer.py` | SMTP; refuses non-`.example` recipients by default |
| `dlra/api.py`, `dlra/mcp_server.py` | Supplier API and MCP tools for cards and cases |

## Synthetic demo sources

`data/demo/` holds what a small supplier would actually have for the six cases: a Tally-style ledger export,
a bank statement, invoice, purchase order and delivery PDFs, `.eml` files, one buyer payment report, and two
hand-written stand-ins for integration responses. All of it is generated from `data/demo/world.yaml`:

```
uv run python -m scripts.make_demo_data
```

The engine does not parse these files yet. The replay below still uses `scripts/demo_world.py`.

The diagnosis, extraction and drafting layer and the web pages are not built yet. The demo replay uses
scripted diagnoses and already-extracted evidence, and drafts are placeholders.

## MCP

`.mcp.json` registers the tools for Claude Code. They call a running API with `OPS_TOKEN`. With no token set,
the API refuses every request.

Where the conduct rules would block a reply that resolves the thing that caused them, the engine allows
only that reply: a bank-reference request after a reported payment, and dispute messages during a dispute.
Opt-out and a human pause block everything.

## Deploy

`render.yaml` describes the Render web service. It serves the supplier API over the demo cases with scripted
diagnoses and sends no mail. `/healthz` is public; everything under `/api` needs `OPS_TOKEN`. The database is
rebuilt on every boot.
