-- Timestamps are ISO-8601 UTC. Money is integer paise. JSON is stored as TEXT.

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
  dedup_key TEXT NOT NULL UNIQUE, case_id TEXT, payload TEXT NOT NULL DEFAULT '{}',
  produced_by_agent INTEGER NOT NULL DEFAULT 0, triggers_run INTEGER NOT NULL
);
-- The event log is append-only, enforced by the database and not just by convention.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

-- Events the dispatcher has handled. Kept apart because events themselves never change.
CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY REFERENCES events(id));

CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, gstin TEXT, entity_type TEXT, aliases TEXT DEFAULT '[]',
  tds_rate_bp INTEGER CHECK (tds_rate_bp IN (0, 10, 100, 200, 1000)),
  tds_basis TEXT, payment_history TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS contacts (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id), name TEXT, email TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'ledger', source_evidence_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('verified', 'unverified', 'conflicting')),
  opted_out INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY, number TEXT NOT NULL, customer_id TEXT NOT NULL REFERENCES customers(id),
  po_number TEXT, issue_date TEXT, due_date TEXT, accepted_date TEXT,
  taxable_paise INTEGER NOT NULL, gst_paise INTEGER NOT NULL, gross_paise INTEGER NOT NULL,
  credit_note_paise INTEGER NOT NULL DEFAULT 0, msmed_eligible INTEGER NOT NULL DEFAULT 0,
  UNIQUE (number, customer_id)
);

CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL UNIQUE REFERENCES invoices(id), state TEXT,
  diagnosis_confidence REAL, under_review INTEGER NOT NULL DEFAULT 0, human_pause INTEGER NOT NULL DEFAULT 0,
  safe_pause_until TEXT, diagnosis TEXT, running INTEGER NOT NULL DEFAULT 0, rerun_pending INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, source_type TEXT NOT NULL, source_ref TEXT, ts TEXT,
  field TEXT, claim TEXT, value TEXT, confidence REAL,
  status TEXT NOT NULL CHECK (status IN ('verified', 'unverified', 'conflicting', 'expired_unverified')),
  influenced_decision_id TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, trigger_event_id TEXT, started_at TEXT, finished_at TEXT,
  status TEXT
);

CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, rule_id TEXT,
  evidence_ids TEXT DEFAULT '[]', payload TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'approved', 'rejected', 'changes_requested')),
  decided_by TEXT, decided_at TEXT, note TEXT, ops_ref TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT NOT NULL, state TEXT,
  confidence REAL, action TEXT NOT NULL, outcome TEXT NOT NULL, allowed INTEGER NOT NULL,
  probe INTEGER NOT NULL DEFAULT 0, rule_id TEXT, reason TEXT, evidence_ids TEXT DEFAULT '[]',
  gates TEXT DEFAULT '{}', card_id TEXT REFERENCES cards(id)
);

CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, utr TEXT, amount_paise INTEGER NOT NULL, value_date TEXT NOT NULL, narration TEXT,
  payer_name TEXT, source TEXT, dedup_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS allocations (
  id TEXT PRIMARY KEY, payment_id TEXT NOT NULL, invoice_id TEXT NOT NULL, candidate INTEGER NOT NULL,
  amount_paise INTEGER NOT NULL, tds_reported_paise INTEGER NOT NULL DEFAULT 0,
  tds_evidenced_paise INTEGER NOT NULL DEFAULT 0, confidence REAL, basis TEXT DEFAULT '[]',
  status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
  UNIQUE (payment_id, invoice_id, candidate)
);

CREATE TABLE IF NOT EXISTS promises (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, data TEXT NOT NULL,
  baseline_paise INTEGER NOT NULL DEFAULT 0,   -- confirmed cash already received when it became active
  status TEXT NOT NULL
    CHECK (status IN ('proposed', 'awaiting_confirmation', 'active', 'broken', 'fulfilled', 'declined'))
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, contact_id TEXT, direction TEXT NOT NULL DEFAULT 'out',
  action TEXT, intent TEXT, subject TEXT, body TEXT, facts_text TEXT DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('drafted', 'queued', 'blocked', 'sent', 'received')),
  rule_id TEXT, simulated INTEGER NOT NULL DEFAULT 0, sent_at TEXT
);

CREATE TABLE IF NOT EXISTS magic_links (
  token_sha256 TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id TEXT PRIMARY KEY, prompt_sha256 TEXT, provider TEXT, model TEXT, latency_ms INTEGER,
  cached INTEGER, ts TEXT
);
