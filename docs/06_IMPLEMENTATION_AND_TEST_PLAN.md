# 06 — Implementation, Test, Installation and Acceptance Plan

## 1. Phased Implementation Plan

Each phase ends with: list of completed files, tests run (real output), failures, remaining work. No test is reported as passing unless executed.

| Phase | Scope | Key files | Exit criteria |
|-------|-------|-----------|---------------|
| 1 Architecture & threat model | Interfaces, models, constraints, dependency pins | docs/, `pyproject.toml`, `config.py`, `domain/models.py` | Docs reviewed; pins chosen from a real install |
| 2 Core financial engine | Money, normalization, DB, migrations, repositories | `domain/*`, `database/*`, `services/normalization.py` | Unit tests green: money, parsing, DB, migrations |
| 3 Import pipeline | CSV/XLSX → PDF → OCR; preview, mapping, validation, rollback | `importer/*`, `security/file_security.py`, `pages/0_Import_Center.py` | Synthetic statements import end-to-end; rollback test |
| 4 Categorization | Rules, mappings, LLM client, validators, review UI | `services/categorization.py`, `ai/local_llm.py`, `ai/prompts.py`, `ai/response_validator.py` | Malformed/injected outputs rejected; works with mock and without LLM |
| 5 Analytics & anomalies | Metrics, transfers, recurring, reconciliation, alerts | `services/analytics.py`, `reconciliation.py`, `duplicates.py`, `anomaly_detection.py`, `recurring.py` | Monthly totals equal independent expected values |
| 6 Dashboard & assistant | Pages, query planner/executor | `app.py`, `pages/*`, `ai/query_*` | Approved questions answered with evidence; unauthorized plans rejected |
| 7 Security hardening | Logging, redaction, network guard, backup, export safety, offline verification | `security/*`, `services/backup.py`, `scripts/verify_offline.py` | Security tests green; offline run documented |
| 8 Testing & packaging | Full test run, docs, bundle script, checklist | README/SECURITY/PRIVACY, `scripts/*` | All acceptance criteria evidenced |

Suggested order inside Phase 2: `money.py` → `models.py` → `connection.py` + `schema.sql` + `migrations.py` → `repositories.py` → `normalization.py`.

Parsing of untrusted files (Phase 7 option): run importers in a child process with CPU/time/memory limits so a parser crash or hang cannot take down the UI.

## 2. Test Strategy

Principles: synthetic data only; deterministic fixtures; an independent oracle for expected totals (hand-written expected values in test files, computed separately from application code); property-based tests for money parsing; mocked Ollama via a local fake HTTP server or `httpx.MockTransport`.

### 2.1 Test matrix

| Area (prompt §14) | Level | Test ideas |
|-------------------|-------|-----------|
| Positive/negative amounts | Unit | `(12.30)`, `-12.30`, `12.30-`, `12.30 DR` all → −1230 |
| Debit/credit interpretation | Unit | Separate columns vs. signed; credit-card inversion; user override respected |
| Decimal precision | Unit/property | Sum of 10,000 random amounts equals integer sum; never float; `0.1+0.2` case |
| Multiple currencies | Unit | Mixed-currency list yields per-currency results; adding Money of different currencies raises |
| Date parsing/ambiguity | Unit | `01/02/2024` ambiguous → requires choice; `25/01/2024` resolves DMY; two-digit years flagged |
| CSV/Excel import | Integration | BOM, `;` delimiter, preamble rows, footer totals, merged cells, multiple sheets |
| PDF extraction failures | Integration | Image-only PDF, encrypted PDF, corrupt file, 0 tables → clean error |
| OCR errors/confidence | Integration (skipped if no Tesseract) | Low-confidence rows flagged, amounts require confirmation |
| Duplicate detection | Unit | Identical legit rows with occurrence index not auto-dropped; candidates surfaced; nothing deleted |
| Transfers/refunds | Unit | Matched transfer excluded from income/expense; refund nets category; unmatched flagged |
| Monthly aggregation | Unit | Month boundaries (31st/1st), leap day, time-zone independence |
| Savings rate | Unit | Income=0 → unavailable with reason; partial month → unavailable; valid case equals oracle |
| Balance reconciliation | Unit | Deposit vs. card sign rules; missing balance → `not_available`; mismatch position reported; newest-first ordering |
| Malformed AI responses | Unit | Invalid JSON, wrong types, missing indices, duplicate indices, oversize reason |
| Invalid categories | Unit | Unknown/disabled category rejected → needs_review |
| Prompt injection | Unit/integration | Hostile descriptions; mock LLM tries to return `Salary` or extra tools; result validated |
| Unauthorized queries | Unit | Unknown plan name, SQL in parameters, out-of-range `n`, unknown category, write attempt on RO connection fails |
| File size/extension | Security | 0-byte, oversize, `.exe` renamed `.csv`, magic mismatch, double extension, traversal names |
| Formula injection | Security | Descriptions starting `=`, `+`, `-`, `@`, tab, CR neutralized in CSV/XLSX export |
| Import rollback | Integration | Rollback restores analytics to pre-import numbers; other batches untouched; audit event present |
| Backup/restore | Integration | Round trip equality; wrong passphrase fails; tampered file fails (AES-GCM); integrity check after restore |
| Offline operation | System | App flow with network blocked; static scan finds no remote references; guard rejects non-loopback sockets |
| AI unavailable | Integration | Import, search, rules, analytics, assistant fallback all work with LLM endpoint refusing connections |
| Logs | Security | Run workflow with canary strings in descriptions; assert none appear in logs |
| Migrations | Integration | Fresh = upgraded schema; refuse newer DB version |
| Read-only assistant | Security | `INSERT`/`UPDATE` via assistant connection raises |
| Streamlit config | Security | Config has loopback address, stats disabled, XSRF on |
| Permissions | Security (POSIX; Windows ACL check best effort) | Data dir 0700, DB 0600 |

### 2.2 Tools and commands (to be run and reported for real)
```
pytest -q tests/unit
pytest -q tests/integration
pytest -q tests/security
pytest -q --cov=src/bank_analyzer --cov-report=term-missing
pytest -m live_llm      # optional, needs installed model
ruff check . && ruff format --check . && mypy src
pip-audit -r requirements.txt     # online build step only
```
Markers: `live_llm`, `ocr`, `slow`. Coverage target: ≥ 90% for `domain/`, `services/analytics.py`, `reconciliation.py`, `security/`; ≥ 80% overall.

### 2.3 Synthetic fixtures (`tests/fixtures/`)
- `deposit_basic.csv` (US format), `deposit_eu.csv` (`;`, `1.234,56`, DMY), `card_statement.csv` (inverted signs), `two_columns_debit_credit.csv`, `multi_currency.csv`, `with_preamble_footer.csv`, `ambiguous_dates.csv`, `injection_descriptions.csv`, `formula_descriptions.csv`.
- XLSX generated by a fixture builder script (openpyxl), including a multi-sheet and a merged-header file.
- PDFs generated by a script (reportlab or fpdf2, dev-only dependency): text-based statement, image-only page, encrypted, corrupt.
- Each fixture has a sibling `*.expected.json` holding independently computed totals.

## 3. Installation and Operation (content outline for README)

Common: Python 3.11/3.12, `python -m venv .venv`, `pip install -r requirements.txt`, install Ollama, `ollama pull <model>`, optional Tesseract.

| Step | Windows 10/11 (PowerShell) | macOS | Linux |
|------|-----------------------------|-------|-------|
| venv | `py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1` | `python3.11 -m venv .venv && source .venv/bin/activate` | same as macOS |
| Ollama | Installer from ollama.com (verified download), set user env `OLLAMA_HOST=127.0.0.1:11434`, `OLLAMA_NO_CLOUD=1` | `brew install ollama` or app; `launchctl setenv` | Official install script or package; `systemctl edit ollama` Environment lines |
| Tesseract (optional) | UB Mannheim build or winget | `brew install tesseract` | `apt install tesseract-ocr` |
| Disk encryption | BitLocker | FileVault | LUKS |
| Firewall | Defender outbound rules | pf / LuLu / Little Snitch | ufw / nftables |
| Start | `streamlit run app.py` (config pins address) | same | same |
| Stop | Ctrl+C in terminal; stop Ollama service | same | same |
| Data location | `%LOCALAPPDATA%\BankAnalyzer` (outside OneDrive) | `~/Library/Application Support/BankAnalyzer` | `~/.local/share/bankanalyzer` |

### Offline installation bundle
1. On an online machine of the same OS/arch/Python version: `pip download -r requirements.txt -d wheelhouse` (include transitive dependencies; use `--require-hashes` lock file).
2. Copy Ollama installer, model weights (`ollama pull` then copy the models directory or use `ollama create` from a GGUF), Tesseract installer plus `eng.traineddata`, and the wheelhouse to removable media; record SHA-256 checksums.
3. On the offline machine: `pip install --no-index --find-links wheelhouse -r requirements.txt`.
4. Run `python scripts/setup_environment.py` (creates data dir with permissions, initializes DB) and `python scripts/check_model.py` (reports: Ollama reachable?, model present?, core features available without it).
`scripts/make_offline_bundle.py` automates steps 1–2 for the online preparation machine.

### Model replacement
Settings → select installed model (list from local `/api/tags`); `check_model.py` validates JSON-schema output on a 5-item self-test before enabling; previous suggestions remain tagged with the model name and digest.

### Backup / restore / deletion
- Backup: SQLite online backup API → file → AES-GCM encrypt (scrypt) → `*.bankbak`; verify by decrypt + `integrity_check` on temp copy.
- Restore: validate, decrypt, integrity check, then swap atomically after backing up the current DB.
- Deletion: batch rollback, purge, full wipe (with disclosure of SSD/sync/backup limitations).

## 4. Troubleshooting Guide (outline)

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| "AI offline" badge | Ollama not running / model missing | Start Ollama; `ollama list`; `scripts/check_model.py` |
| Categorization slow | Model too large for CPU | Choose smaller model; reduce batch size |
| Import: dates wrong | Ambiguous format | Choose DMY/MDY in mapping step |
| Import: amounts 100× off | Wrong decimal separator | Set separators in mapping |
| PDF yields no rows | Scanned PDF or unusual layout | Enable OCR, or export CSV from bank |
| OCR unavailable | Tesseract not installed or not on PATH | Install; set `BANKAN_TESSERACT_CMD` |
| Port in use | Another Streamlit instance | Change `server.port` in `.streamlit/config.toml` |
| DB locked | Two app instances / backup running | Close other instance; wait |
| Version newer than app | DB from a newer build | Upgrade app or restore backup |
| Offline check fails | Remote reference found | Read report; remove/vendor asset |
| Windows permission warning | ACL broader than user | Run `icacls` fix suggested in Settings |

## 5. Acceptance Criteria Traceability

| Prompt §19 criterion | Evidence |
|----------------------|----------|
| Import synthetic statement and preview | Integration test + manual run notes |
| Correct column mappings | UI test / mapping unit tests |
| Review and categorize | Categorization + review queue tests |
| Monthly totals match independent results | `*.expected.json` oracle tests |
| Duplicate detection never silently deletes | Unit test asserting row counts unchanged |
| Assistant answers approved questions | Integration tests with mock LLM |
| Invalid model responses can't corrupt data | Validator and DB-state tests |
| Useful without AI | AI-down integration test |
| No external services in normal operation | Offline procedure results |
| Not exposed to the network | Config test + `ss`/`netstat` check |
| Export, backup, restore, deletion | Integration tests |
| Tests cover core finance + security | Coverage report |
| Offline operation tested and documented | Offline report in docs |
| Limitations disclosed | SECURITY.md residual risks |

## 6. Feature Checklist (initial state — nothing implemented yet)

Legend: ☐ unimplemented, ◐ partial, ☑ implemented, ✔ tested. Update only from real evidence.

| Feature | Status |
|---------|--------|
| Money type and parsing | ✔ (Phase 2) |
| Canonical model + validation | ✔ (Phase 2) |
| SQLite schema + migrations | ✔ (Phase 2) |
| Repositories + rollback | ✔ (Phase 2) |
| CSV import | ✔ (Phase 3) |
| XLSX import | ✔ (Phase 3). Tested only on openpyxl-made files |
| PDF text import | ◐ (Phase 3). Ruled tables are read and tested; text-aligned layouts that fragment cells are refused by design; no Phase-7 subprocess isolation yet |
| OCR import | ◐ (Phase 3). TSV parsing, table building and confidence gating are tested; the real Tesseract call is NOT verified (Tesseract not installed during development; one optional `ocr` test is skipped) |
| Column mapping + profiles | ◐ (Phase 3). Header-based suggestions and balance-based sign hints done; saved bank profiles not implemented |
| Duplicate detection | ◐ (Phase 3). Import-time candidates against stored data are done; recurring/within-history duplicate analysis is Phase 5 |
| Transfer matching | ☐ |
| Balance reconciliation | ☐ |
| Rule-based categorization | ☐ |
| LLM categorization | ☐ |
| Review queue + bulk actions | ☐ |
| Analytics catalogue | ☐ |
| Recurring detection | ☐ |
| Anomaly detection | ☐ |
| Dashboard pages | ☐ |
| AI assistant (planner/executor/validator) | ☐ |
| Export with formula protection | ☐ |
| Backup/restore (encrypted) | ☐ |
| Logging/redaction | ☐ |
| Network guard + offline verification | ☐ |
| README / SECURITY / PRIVACY | ☐ |
| Offline bundle script | ☐ |

## 7. Open Decisions Needing the Owner's Input (non-blocking; defaults in brackets)

1. Default currency and locale [user setting; no default guess].
2. LLM model family/size [pick from installed hardware; small instruct model].
3. Backup encryption tool [`cryptography` AES-GCM + scrypt].
4. Optional start-up passcode for the web UI [off by default, recommended on shared machines].
5. Whether the account alias may include last-4 digits [optional, off].
6. Repository path is inside OneDrive: confirm that source code may stay there but the data directory must not [yes].
7. Whether to run parsers in an isolated subprocess [yes in Phase 7].
| Import preview + confirmation gates | ✔ (Phase 3, service layer; the Import Center page is Phase 6) |
