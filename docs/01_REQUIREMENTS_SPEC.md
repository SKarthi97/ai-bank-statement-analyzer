# 01 — Requirements Specification

Priority: **M** = must (non-negotiable or acceptance-critical), **S** = should, **C** = could.
Each requirement has a verification method: **T** test, **I** inspection/review, **D** documentation, **O** offline procedure.

## 1. Privacy and Offline Operation (PRV)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| PRV-01 | Runtime operation requires no internet after install and model download | M | O |
| PRV-02 | No statement data, transactions, identifiers, summaries, prompts or model output leave the machine | M | O, I |
| PRV-03 | No OpenAI/cloud LLM, hosted OCR, cloud DB, telemetry or third-party financial APIs | M | I |
| PRV-04 | LLM runs via Ollama (or equally auditable local runtime) | M | I |
| PRV-05 | Web UI and AI API bind to `127.0.0.1` by default; no LAN/public exposure | M | T, O |
| PRV-06 | No collection of banking credentials, PINs, CVVs, tokens | M | I |
| PRV-07 | No bank login, payment, or transfer automation | M | I |
| PRV-08 | No remote fonts/scripts/images/CDN at runtime; no update checks | M | O |
| PRV-09 | Streamlit usage statistics disabled | M | I |
| PRV-10 | Ollama cloud features disabled (`OLLAMA_NO_CLOUD=1` where supported) and verified per version | M | D, O |
| PRV-11 | Optional outbound-network block (app-level guard plus OS firewall guidance) | S | T, D |
| PRV-12 | Unmet guarantees are documented as limitations, never overclaimed | M | D |

## 2. Import (IMP)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| IMP-01 | Support CSV, XLSX, text-based PDF | M | T |
| IMP-02 | Scanned PDF via optional local OCR (Tesseract); app works without it | S | T |
| IMP-03 | Staged workflow: select → validate → extract → preview → map → normalize → warn → confirm → save → summary | M | T |
| IMP-04 | Original file is never modified; processing uses a private temp copy | M | T |
| IMP-05 | Validate extension, MIME/magic bytes, size limit, structure (zip bomb check for XLSX, page limit for PDF) | M | T |
| IMP-06 | User can map date, description, debit, credit, amount, currency, balance columns | M | T |
| IMP-07 | Handle multiple date formats, decimal/thousands separators, debit/credit columns, negative amounts | M | T |
| IMP-08 | Ambiguous date/amount/sign is flagged and requires explicit user choice | M | T |
| IMP-09 | Saved bank-specific import profiles (column mapping + format hints) | S | T |
| IMP-10 | Show validation warnings and suspected duplicates before confirm | M | T |
| IMP-11 | Import is atomic (one DB transaction) | M | T |
| IMP-12 | Import history and rollback of a whole batch | M | T |
| IMP-13 | Do not store full account numbers; use user-chosen alias, optional last-4 | M | T, I |
| IMP-14 | Preserve source row reference (file hash, sheet/page, row index) | M | T |
| IMP-15 | OCR low-confidence rows are flagged for mandatory review | M | T |
| IMP-16 | Re-importing the same file hash warns and requires explicit override | S | T |

## 3. Financial Model and Calculation (FIN)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| FIN-01 | Money uses `Decimal` or integer minor units; never `float` | M | T, I |
| FIN-02 | Signed amount convention: credits (money in) positive, debits (money out) negative, per account's *cash-flow perspective* | M | T |
| FIN-03 | Every amount has an ISO 4217 currency; no cross-currency summation without configured static rates | M | T |
| FIN-04 | No online exchange rates | M | I |
| FIN-05 | All calculations are deterministic Python/SQL; LLM output is never used as a number | M | T, I |
| FIN-06 | Every metric exposes its definition, period, currency and contributing transaction IDs | M | T |
| FIN-07 | Defined handling of own-account transfers, credit-card payments, refunds, reversals, cash withdrawals to avoid double counting | M | T |
| FIN-08 | Savings rate shown only when valid; otherwise an explanation is shown | M | T |
| FIN-09 | Balance reconciliation distinguishes "no balance data" from "reconciliation failed", with account-type sign rules | M | T |
| FIN-10 | Never invent missing transactions, balances or categories | M | T, I |

## 4. Categorization (CAT)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| CAT-01 | Precedence: user rules → approved merchant mappings → built-in rules → local AI → Needs Review | M | T |
| CAT-02 | Editable default category list; create, rename, merge, disable | M | T |
| CAT-03 | AI output constrained to JSON schema with allowed category enum; unknown or malformed output rejected | M | T |
| CAT-04 | AI receives minimum context (normalized description, amount sign, optionally currency); no account data | M | T, I |
| CAT-05 | Descriptions are treated as untrusted; embedded instructions are ignored | M | T |
| CAT-06 | Explicit `Needs Review` state when uncertain | M | T |
| CAT-07 | Model confidence labelled as uncalibrated; never shown as probability | M | I |
| CAT-08 | Bulk approve / bulk correct | M | T |
| CAT-09 | User-approved merchant mappings persisted and take precedence | M | T |
| CAT-10 | User-approved categories are never silently overwritten by re-categorization | M | T |
| CAT-11 | Categorization works (rules only) with the LLM down | M | T |

## 5. Analytics (ANA)

Metrics required: total credits/debits; monthly income/expenses/net; category spend; month-over-month change;
trends; savings rate; recurring candidates; subscription amount changes; largest transactions; merchant summaries;
cash withdrawals; refund tracking; duplicate candidates; unusual amounts; account balance reconciliation.

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| ANA-01 | Each metric is a pure, unit-tested function over transaction records | M | T |
| ANA-02 | Monthly totals match independently computed expected values on synthetic fixtures | M | T |
| ANA-03 | Results carry `definition`, `period`, `currency`, `transaction_ids` | M | T |
| ANA-04 | Recurring detection uses configurable cadence and tolerance | S | T |

## 6. AI Assistant (AST)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| AST-01 | Pipeline: question → validated query plan → deterministic execution → minimal evidence → grounded answer | M | T |
| AST-02 | LLM cannot generate or run SQL, Python, shell, filesystem or network operations | M | T, I |
| AST-03 | Allowlisted query catalogue with typed, range-checked parameters | M | T |
| AST-04 | Assistant database connection is read-only (`mode=ro` URI and `PRAGMA query_only`) | M | T |
| AST-05 | Unmappable questions yield a clarification request or limitation message | M | T |
| AST-06 | Answers include period, currency and supporting transaction references | M | T |
| AST-07 | Answers state data gaps; no fabricated causes | M | T |
| AST-08 | Post-generation validator checks every number in the answer against computed results | S | T |
| AST-09 | When LLM is unavailable, UI falls back to showing the computed result with a template summary | M | T |

## 7. Anomalies and Duplicates (ANO)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| ANO-01 | Rule-based detectors: large amount vs. history, unusual frequency, duplicate charges, recurring increase, new merchant, unexpected negative balance | M | T |
| ANO-02 | Configurable thresholds; minimum-history guard | M | T |
| ANO-03 | Each alert stores rule id, trigger values and comparison baseline | M | T |
| ANO-04 | Alerts can be dismissed or confirmed; history kept locally | M | T |
| ANO-05 | Wording avoids definitive fraud claims | M | I |
| ANO-06 | Duplicates are shown as candidate pairs; never auto-deleted | M | T |
| ANO-07 | No automated financial action of any kind | M | I |

## 8. User Interface (UI)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| UI-01 | Overview: income, expenditure, net, savings rate, trend chart, category breakdown, recent transactions, alerts | M | T/I |
| UI-02 | Transactions: search, date/category/merchant filters, sort, pagination, inline category edit, detail, CSV export | M | T |
| UI-03 | Analytics: month comparison, category trends, merchants, recurring, cash flow, reconciliation | M | T |
| UI-04 | AI Assistant page with evidence references | M | T |
| UI-05 | Import Center: preview, mapping, errors, duplicates, history, rollback | M | T |
| UI-06 | Settings/Privacy: model status, DB path, data dir, export, delete, offline status, retention | M | T |
| UI-07 | Confirmation prompts for destructive actions; clear error messages; empty states; accessible controls | M | I |
| UI-08 | No business logic in Streamlit page files | M | I |
| UI-09 | CSV export neutralizes spreadsheet formula injection | M | T |

## 9. Persistence (DB)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| DB-01 | Versioned schema with migrations | M | T |
| DB-02 | Tables: accounts, transactions, categories, categorization rules, import batches, recurring candidates, anomaly alerts, settings, audit events | M | T |
| DB-03 | Parameterized SQL only; foreign keys on | M | T, I |
| DB-04 | Multi-step writes inside transactions | M | T |
| DB-05 | Batch rollback preserves traceability and leaves other data intact | M | T |
| DB-06 | Integrity check (`PRAGMA integrity_check`, `foreign_key_check`) available from Settings | S | T |
| DB-07 | Backup/restore with verification; encrypted backups | M | T |
| DB-08 | Audit events contain no sensitive content (no descriptions, amounts, account data) | M | T |

## 10. Security (SEC)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| SEC-01 | Strict file size and type limits; safe parser configuration | M | T |
| SEC-02 | Path traversal and unsafe filename protection | M | T |
| SEC-03 | Secure temp files (private dir, restricted permissions, cleanup, even on error) | M | T |
| SEC-04 | Logs never contain financial data or secrets; redaction utility | M | T |
| SEC-05 | Restricted permissions on data directory (0700 / ACL) | M | T |
| SEC-06 | Pinned dependencies; vulnerability review procedure | M | D |
| SEC-07 | Threat model and residual risk list | M | D |
| SEC-08 | SQLite is not claimed to be encrypted; optional encryption documented honestly | M | D |
| SEC-09 | Output encoding for any HTML rendering; avoid `unsafe_allow_html` with untrusted text | M | I, T |

## 11. Quality, Packaging and Docs (QA)

| ID | Requirement | Pri | Verify |
|----|-------------|-----|--------|
| QA-01 | Type hints, docstrings on public functions, consistent linting/formatting | M | I |
| QA-02 | Synthetic fixtures only; no real data in repo | M | I |
| QA-03 | Unit, integration (mock Ollama), security, offline tests | M | T |
| QA-04 | Test confirming core features work with AI service down | M | T |
| QA-05 | `requirements.txt` with tested pins; offline bundle instructions | M | D |
| QA-06 | Per-OS install, run, stop, firewall, backup, deletion, troubleshooting docs | M | D |
| QA-07 | Model availability check script (`scripts/verify_offline.py` or similar) | M | T |
| QA-08 | Feature checklist with honest status | M | D |
| QA-09 | Test results reported only from real executions | M | I |

## 12. Non-Functional Targets (proposed, not in prompt)

| Area | Target |
|------|--------|
| Import performance | 10,000-row CSV parsed and previewed in under 10 s on a modest laptop |
| UI responsiveness | Transaction page filter/sort under 1 s for 100k rows (indexed queries, server-side pagination) |
| Memory | Under 1 GB for app excluding the LLM |
| LLM batching | Configurable batch size; per-request timeout; cancellable |
| Hardware | CPU-only viable; model choice in settings |
| Max upload | Default 25 MB CSV/XLSX, 50 MB PDF, 100 PDF pages (configurable) |
