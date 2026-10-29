-- Schema version 1.
-- Applied by migrations.py statement by statement inside one transaction.
-- Do not edit after release: add a new migration instead.
-- Avoid semicolons inside comments (the statement splitter relies on sqlite3.complete_statement).

CREATE TABLE schema_version (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  alias TEXT NOT NULL UNIQUE,
  account_type TEXT NOT NULL CHECK (account_type IN ('deposit','credit_card','loan','cash','investment')),
  currency TEXT NOT NULL CHECK (length(currency) = 3),
  last4 TEXT CHECK (last4 IS NULL OR length(last4) = 4),
  opening_balance_minor INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at TEXT NOT NULL
);

CREATE TABLE categories (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('income','expense','transfer','neutral')),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
  merged_into_id TEXT REFERENCES categories(id),
  created_at TEXT NOT NULL
);

CREATE TABLE import_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  mapping_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE import_batches (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  file_sha256 TEXT NOT NULL,
  file_kind TEXT NOT NULL CHECK (file_kind IN ('csv','xlsx','pdf','pdf_ocr')),
  display_name TEXT,
  row_count_read INTEGER NOT NULL CHECK (row_count_read >= 0),
  row_count_imported INTEGER NOT NULL CHECK (row_count_imported >= 0),
  row_count_skipped INTEGER NOT NULL CHECK (row_count_skipped >= 0),
  warnings_json TEXT,
  status TEXT NOT NULL CHECK (status IN ('committed','rolled_back')),
  created_at TEXT NOT NULL,
  rolled_back_at TEXT
);

CREATE INDEX idx_batches_hash ON import_batches(file_sha256);

CREATE TABLE transactions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  import_batch_id TEXT NOT NULL REFERENCES import_batches(id),
  txn_date TEXT NOT NULL,
  booking_date TEXT,
  original_description TEXT NOT NULL,
  normalized_description TEXT NOT NULL,
  merchant_key TEXT,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL CHECK (length(currency) = 3),
  reported_balance_minor INTEGER,
  txn_type TEXT NOT NULL CHECK (txn_type IN ('purchase','income','transfer_in','transfer_out','cash_withdrawal','refund','reversal','fee','interest','loan_payment','card_payment','other')),
  category_id TEXT REFERENCES categories(id),
  category_source TEXT NOT NULL DEFAULT 'none' CHECK (category_source IN ('none','user_manual','user_rule','merchant_mapping','builtin_rule','llm')),
  review_status TEXT NOT NULL DEFAULT 'needs_review' CHECK (review_status IN ('ok','needs_review','approved','rejected_suggestion')),
  transfer_group_id TEXT,
  source_ref_json TEXT NOT NULL,
  occurrence_index INTEGER NOT NULL DEFAULT 0 CHECK (occurrence_index >= 0),
  is_voided INTEGER NOT NULL DEFAULT 0 CHECK (is_voided IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK ((category_id IS NULL) = (category_source = 'none'))
);

CREATE INDEX idx_txn_date ON transactions(txn_date);
CREATE INDEX idx_txn_account_date ON transactions(account_id, txn_date);
CREATE INDEX idx_txn_category ON transactions(category_id);
CREATE INDEX idx_txn_merchant ON transactions(merchant_key);
CREATE INDEX idx_txn_batch ON transactions(import_batch_id);
CREATE INDEX idx_txn_review ON transactions(review_status);

CREATE TABLE categorization_rules (
  id TEXT PRIMARY KEY,
  priority INTEGER NOT NULL,
  match_type TEXT NOT NULL CHECK (match_type IN ('contains','starts_with','regex','merchant_key')),
  pattern TEXT NOT NULL,
  amount_sign TEXT CHECK (amount_sign IN ('in','out')),
  category_id TEXT NOT NULL REFERENCES categories(id),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at TEXT NOT NULL
);

CREATE TABLE merchant_mappings (
  merchant_key TEXT PRIMARY KEY,
  category_id TEXT NOT NULL REFERENCES categories(id),
  approved_at TEXT NOT NULL
);

CREATE TABLE recurring_candidates (
  id TEXT PRIMARY KEY,
  merchant_key TEXT NOT NULL,
  currency TEXT NOT NULL,
  cadence_days INTEGER NOT NULL,
  typical_amount_minor INTEGER NOT NULL,
  last_amount_minor INTEGER NOT NULL,
  occurrences INTEGER NOT NULL,
  first_date TEXT NOT NULL,
  last_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('candidate','confirmed','dismissed')),
  updated_at TEXT NOT NULL
);

CREATE TABLE anomaly_alerts (
  id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  transaction_id TEXT REFERENCES transactions(id),
  related_transaction_ids_json TEXT,
  details_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','dismissed','confirmed')),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE INDEX idx_alerts_status ON anomaly_alerts(status);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event TEXT NOT NULL,
  entity_id TEXT,
  count INTEGER,
  created_at TEXT NOT NULL
);
