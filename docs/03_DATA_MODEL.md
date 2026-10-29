# 03 — Data Model

## 1. Canonical Transaction (Pydantic / domain model)

| Field | Type | Rules |
|-------|------|-------|
| `id` | UUID4 string | Generated at import |
| `account_id` | FK | Account alias, never a full account number |
| `txn_date` | date (ISO) | Required; ambiguous formats need user confirmation |
| `booking_date` | date or null | Optional |
| `original_description` | str | Verbatim text from source, length-capped (e.g. 1000), control chars stripped |
| `normalized_description` | str | Upper-cased, whitespace-collapsed, noise tokens (card digits, reference numbers) stripped; derived, reproducible |
| `merchant_key` | str or null | Stable key derived from normalized description for mappings |
| `amount_minor` | int | Signed; inflow positive, outflow negative |
| `currency` | str(3) | ISO 4217 upper-case |
| `reported_balance_minor` | int or null | Balance printed on statement after this row |
| `txn_type` | enum | `purchase, income, transfer_in, transfer_out, cash_withdrawal, refund, reversal, fee, interest, loan_payment, card_payment, other` |
| `category_id` | FK or null | Null = uncategorized |
| `category_source` | enum | `none, user_manual, user_rule, merchant_mapping, builtin_rule, llm` |
| `review_status` | enum | `ok, needs_review, approved, rejected_suggestion` |
| `transfer_group_id` | str or null | Links matched own-account transfer legs |
| `import_batch_id` | FK | |
| `source_ref` | JSON | `{file_sha256, sheet_or_page, row_index}` |
| `occurrence_index` | int | Position among otherwise-identical rows in same batch (dedupe aid) |
| `created_at`, `updated_at` | UTC ISO-8601 with `Z` | |

Invariants:
- `amount_minor != 0` unless the statement explicitly shows zero (warn).
- `currency` is in the allowed ISO set.
- `category_source == user_manual` or `review_status == approved` ⇒ never overwritten by automation.
- Raw columns from source are not stored beyond `original_description` and `source_ref`, minimizing sensitive retention.

## 2. Money Handling

- Parse text directly to integer minor units using explicit separators (never a locale guess); no float or Decimal is
  involved in parsing. **Implemented policy (stricter than the first draft):** a value with more decimal places than the
  currency allows is *rejected* (`PrecisionError`), never rounded; trailing zeros (`1.2300`) are accepted. Rounding exists
  only as an explicit opt-in on `Money.from_decimal(..., round_excess=True)`.
- Integer minor units are the stored form. Sum in integers. Convert to `Decimal` only to display.
- Currency exponent table: default 2; JPY/KRW/CLP 0; BHD/KWD/OMR/JOD/TND 3.
- Formats handled: `1,234.56`, `1.234,56`, `1 234,56`, `(123.45)` negative, `123.45-` trailing minus, `123.45 DR/CR`, currency symbols.
- Ambiguity examples that must prompt: `1,234` (thousands or decimal?), `01/02/2024` (DMY vs MDY) when both interpretations are valid across the whole column.
  Column-level inference: if any value in a column is only valid under one format (e.g. `25/01/2024`), propose that format and show the evidence.

## 3. SQLite Schema (v1)

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE schema_version (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  alias TEXT NOT NULL UNIQUE,
  account_type TEXT NOT NULL CHECK (account_type IN ('deposit','credit_card','loan','cash','investment')),
  currency TEXT NOT NULL CHECK (length(currency)=3),
  last4 TEXT CHECK (last4 IS NULL OR length(last4)=4),
  opening_balance_minor INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE categories (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('income','expense','transfer','neutral')),
  is_active INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0,
  merged_into_id TEXT REFERENCES categories(id),
  created_at TEXT NOT NULL
);

CREATE TABLE import_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  mapping_json TEXT NOT NULL,        -- column names/indices, date format, separators, sign convention
  created_at TEXT NOT NULL
);

CREATE TABLE import_batches (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  file_sha256 TEXT NOT NULL,
  file_kind TEXT NOT NULL CHECK (file_kind IN ('csv','xlsx','pdf','pdf_ocr')),
  display_name TEXT,                 -- sanitized base name only, user may blank it
  row_count_read INTEGER NOT NULL,
  row_count_imported INTEGER NOT NULL,
  row_count_skipped INTEGER NOT NULL,
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
  currency TEXT NOT NULL CHECK (length(currency)=3),
  reported_balance_minor INTEGER,
  txn_type TEXT NOT NULL,
  category_id TEXT REFERENCES categories(id),
  category_source TEXT NOT NULL DEFAULT 'none',
  review_status TEXT NOT NULL DEFAULT 'needs_review',
  transfer_group_id TEXT,
  source_ref_json TEXT NOT NULL,
  occurrence_index INTEGER NOT NULL DEFAULT 0,
  is_voided INTEGER NOT NULL DEFAULT 0,   -- set when batch rolled back; excluded from analytics
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_txn_date ON transactions(txn_date);
CREATE INDEX idx_txn_account_date ON transactions(account_id, txn_date);
CREATE INDEX idx_txn_category ON transactions(category_id);
CREATE INDEX idx_txn_merchant ON transactions(merchant_key);
CREATE INDEX idx_txn_batch ON transactions(import_batch_id);
CREATE INDEX idx_txn_review ON transactions(review_status);

CREATE TABLE categorization_rules (
  id TEXT PRIMARY KEY,
  priority INTEGER NOT NULL,                 -- lower runs first
  match_type TEXT NOT NULL CHECK (match_type IN ('contains','starts_with','regex','merchant_key')),
  pattern TEXT NOT NULL,
  amount_sign TEXT CHECK (amount_sign IN ('in','out')),
  category_id TEXT NOT NULL REFERENCES categories(id),
  is_active INTEGER NOT NULL DEFAULT 1,
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
  details_json TEXT NOT NULL,            -- trigger value, baseline, thresholds used
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
```

Seed data: 21 default categories. Implemented kinds: Income and Salary = income; Transfers = transfer; Cash withdrawals,
Refunds, Savings and investments = neutral (reported separately so they are not double counted, see doc 05 section 2);
all others = expense.

Implementation notes (schema v1 as built, `src/bank_analyzer/database/schema.sql`): the schema adds CHECK constraints
beyond the draft above: enum values for `txn_type`, `category_source`, `review_status`, `file_kind`, `status`; and
`CHECK ((category_id IS NULL) = (category_source = 'none'))`, so a category can never exist without a recorded source.

## 4. Design Notes

- **Amount sign vs category kind:** sign is the source of truth for direction; category `kind` is used for report grouping. An outflow categorised as `income` triggers a validation warning.
- **Rollback:** set `import_batches.status='rolled_back'` and mark `transactions.is_voided=1` (default) so that audit trail and traceability remain. A separate "purge" operation hard-deletes with confirmation. All analytics and queries filter `is_voided=0`.
- **Duplicate import protection:** `file_sha256` lookup warns; row-level candidate matching uses (account, date, amount, currency, normalized description, occurrence_index). Not a UNIQUE constraint (see decision D9).
- **User corrections:** updating `category_id` by the user sets `category_source='user_manual'`, `review_status='approved'`; automation skips such rows. Merging categories rewrites rows via explicit repository call inside a transaction and logs an audit event.
- **Foreign keys** enabled per connection. **WAL** optional; document that WAL creates `-wal`/`-shm` files that need the same permissions and must be included in backups (use SQLite backup API for consistency).
- **Read-only connection:** `sqlite3.connect("file:...?mode=ro", uri=True)` plus `PRAGMA query_only=ON`. The assistant executor receives only this connection.
- **Indexes** target date-range, account, category, merchant and review queue queries.

## 5. Migrations

- `schema_version` table with monotonically increasing integer.
- `migrations.py` holds ordered migration functions (`m001_initial`, `m002_...`); each runs in a transaction; migration runner takes an automatic file backup before upgrading.
- Never edit applied migrations; add new ones. Tests: fresh DB reaches latest version; v1 DB with sample data upgrades without data loss.
- Refuse to open a DB whose version is newer than the code understands.

## 6. Entity Relationship Overview

```
accounts 1---* import_batches 1---* transactions *---1 categories
                                      |  \
                                      |   *---* anomaly_alerts
accounts 1---* transactions           |
categories 1---* categorization_rules
categories 1---* merchant_mappings
transactions (merchant_key) ... recurring_candidates (merchant_key, currency)
settings, audit_events, import_profiles: standalone
```

## 7. Data Retention and Deletion

| Item | Retention | Deletion |
|------|-----------|----------|
| Transactions | Until user deletes | Batch rollback (soft), purge (hard), full wipe |
| Import batch metadata | With batches | Purge |
| Logs | Rotating, size-limited (e.g. 5 × 1 MB), no financial content | Delete log dir |
| Temp files | Per import; removed in `finally` and on startup sweep | Automatic |
| Backups | User-managed, encrypted | User deletes; app lists them |
| Full wipe | Overwrites best effort then unlinks DB, WAL, SHM, logs, temp; documents that SSD wear-levelling and OneDrive/cloud sync/backup copies may retain data |

Note for this workspace: the project path is inside OneDrive. The data directory must **not** live in a synced folder; default
to an OS user-data directory outside OneDrive and warn in Settings if the DB path appears to be inside a known sync folder.
