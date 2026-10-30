# 07. Project Status: Built, Pending, Improvements and Testing

Last updated after Phase 8 (2026-10-29). All eight phases are built; the system is **not yet accepted as finished**: see
[08_ACCEPTANCE_REPORT.md](08_ACCEPTANCE_REPORT.md) section 5 for what must still be done. This is a status record, not a plan: update it only from evidence (a test that
ran, a file that exists). Design intent lives in docs 00-06; this file says what is actually true now.

Legend: **Done** = built and tested here. **Written, unverified** = code exists but has not been run in its real
environment. **Pending** = not started.

---

## 1. What was built

| Phase | Area | Status | Main files |
|-------|------|--------|-----------|
| 1 | Project scaffold, settings, tooling | Done | `pyproject.toml`, `config.py`, `.streamlit/config.toml`, `.gitignore` |
| 2 | Core financial engine: money (integer minor units), dates, models, SQLite schema + migrations, repositories, normalization | Done | `domain/*`, `database/*`, `services/normalization.py` |
| 2 | CSV and Excel importers, upload validation (size, magic bytes, macros, zip-bomb, zip-slip) | Done | `importer/csv_importer.py`, `excel_importer.py`, `column_mapper.py`, `security/file_security.py` |
| 3 | Text-PDF import, optional OCR path, import preview and confirmation gates, re-import and duplicate candidates | Done (OCR: see 3) | `importer/pdf_importer.py`, `ocr_importer.py`, `services/import_service.py` |
| 4 | Categorization: user rules, merchant mappings, built-in rules, local-model suggestions, review queue | Done | `services/categorization.py`, `rules.py`, `builtin_rules.py`, `ai/local_llm.py`, `prompts.py`, `response_validator.py`, `selftest.py` |
| 5 | Analytics, transfer matching, refund linking, balance reconciliation, duplicates, recurring payments, alerts A1-A6 | Done | `services/analytics.py`, `transfers.py`, `reconciliation.py`, `duplicates.py`, `recurring.py`, `anomaly_detection.py`, `domain/periods.py` |
| 6 | AI assistant: 11 allowlisted query plans, planner, read-only executor, checked answers | Done (model path: see 3) | `ai/query_plan.py`, `query_planner.py`, `query_executor.py`, `answer.py`, `assistant.py` |
| 6 | Streamlit pages: Overview, Import Center, Transactions + review queue, Analytics, AI Assistant, Settings | **Written, unverified** under real Streamlit | `app.py`, `app_support.py`, `pages/*`, `ui/*` |
| 7 | Scrubbed logging, redaction, application-level network guard, offline verification script | Done (guard unverified with real Streamlit) | `security/redaction.py`, `logging_config.py`, `network_guard.py`, `offline_check.py`, `scripts/verify_offline.py` |
| 7 | Encrypted backup/restore, formula-safe CSV export, full wipe | Done | `services/backup.py`, `exporter.py`, `wipe.py`, `scripts/restore_backup.py` |
| 7 | Parser isolation (child process, timeout, memory cap), optional passcode, permission audit | Done (passcode gate unverified with real Streamlit) | `importer/isolated.py`, `_worker.py`, `security/passcode.py`, `permissions.py` |
| 8 | README, SECURITY, PRIVACY, troubleshooting, firewall guidance, acceptance report | Done | `README.md`, `SECURITY.md`, `PRIVACY.md`, `docs/08_ACCEPTANCE_REPORT.md` |
| 8 | Setup script, safe launcher, offline bundle tooling, database integrity check, pinned dependencies | Done (wheel download never run) | `scripts/setup_environment.py`, `run_app.py`, `make_offline_bundle.py`, `deploy.py`, `database/maintenance.py`, `requirements.txt` |
| 8 | Recurring-payments list on Analytics, integrity button on Settings, `BANKAN_TESSERACT_CMD` | Done (pages unverified under real Streamlit) | `pages/2_Analytics.py`, `pages/4_Settings.py`, `config.py` |

### Properties that hold by design (and are tested)
- Money is an integer plus an ISO currency. No floats in calculations. Currencies are never added together.
- Amounts, dates and separators are never guessed: ambiguity is rejected or put to the user.
- Imports save nothing until the user confirms; every risk (rejected rows, low-confidence OCR, repeated file, suspected
  duplicates) needs an explicit acknowledgement. Duplicates are candidates only; nothing is deleted. Imports can be undone.
- Categories from rules or the model are suggestions until approved. A user's choice is never overwritten by automation.
- Transfers between own accounts are excluded from income and spending only when matched conservatively (unique 1-to-1 pair
  with transfer evidence).
- Alerts describe, never accuse, and confirm/dismiss only record a decision.
- The assistant cannot write (read-only connection), cannot run SQL (fixed plans), and its wording is checked against the
  computed numbers; the evidence is always shown.
- Everything is local: model server must be loopback, proxies ignored, redirects refused, no telemetry.

---

## 2. What is pending

### Roadmap
All eight phases are built. What remains is verification and tuning, not construction (below).

### Phase 8 items not done
- `pip download` for the offline bundle, `pip-audit`, and hash generation (all need the internet).
- Streamlit is not pinned (could not be installed here).
- macOS and Linux instructions are written but untested.
- Memory use was not measured (the doc 01 target of under 1 GB).
- Per `docs/08_ACCEPTANCE_REPORT.md`: saved import profiles (IMP-09), UI for category rename/merge (CAT-02), alert-threshold and
  retention settings (ANO-02, UI-06), and the Overview page's trend chart, category breakdown and recent transactions (UI-01).

### Phase 7 items not done
- The **dynamic** offline check (watching real connections while the app runs, docs/04 section 6) was not performed or automated.
- **XLSX export** (CSV only), and restore from the UI (CLI only: `scripts/restore_backup.py`).
- Optional SQLite encryption (not claimed; use full-disk encryption).
- Saved data-retention settings.

### Follow-through (needed before calling the system done; mirrors the acceptance report)
0. Phase 7 added three things that run inside the page runner and have never run under real Streamlit: the outbound
   network guard, the passcode gate and the Settings security sections. If the app will not start, set
   `BANKAN_BLOCK_OUTBOUND=0` and report it: the guard is the most likely cause.
1. Install Streamlit yourself and run `python -m pytest tests/ui`, then `streamlit run app.py` and click through every page.
2. Pin the passing `streamlit` and `pandas` versions in `requirements.txt` (currently a note, not a pin).
3. Fix whatever the first real run reveals: expect some API-level fixes, since the pages have only run against a stand-in.

### Features documented but not built
- Saved bank import profiles (so a repeat import skips the mapping step).
- UI for managing transfer links, and for renaming or merging categories (services exist; no page).
- CSV export (Phase 7 requires formula-injection protection first).
- Follow-up questions in the assistant ("and the month before?"). Each question stands alone.
- Partial-refund linking (deliberately not guessed).

---

## 3. What has NOT been verified

| Item | State |
|------|-------|
| Streamlit pages under real Streamlit | Run only against a stand-in; `tests/ui/test_pages_apptest.py` has never run |
| Real Ollama | Client and assistant tested against a fake HTTP server and fake models only |
| Real Tesseract OCR | The one live OCR test is skipped (Tesseract not installed) |
| XLSX produced by real Excel | Tested on files generated by openpyxl |
| Real bank statements | All fixtures are synthetic; parser thresholds and alert defaults are reasoned, not tuned on real data |
| Network guard with real Streamlit | Tested against sockets directly; never run inside a Streamlit server |
| Passcode gate and Settings security page | Run against the stand-in only |
| Memory cap on Linux/macOS | `RLIMIT_AS` path written, never run (Windows job-object path tested) |
| Windows ACL audit on other languages | Looks for English group names only |
| Dynamic offline check | Not performed |

---

## 4. Improvements worth making

Ordered by value to a real user.

1. **Tune on real data.** Import a few of your own statements and review: column detection, categories, the alert
   thresholds (A1-A6), recurring detection and the refund window. Adjust defaults from evidence.
2. **Saved import profiles.** Remove the repeated column/format questions for each bank.
3. **Merchant-name quality.** Recurring detection and merchant summaries rely on merchant keys; a merchant whose descriptions
   vary a lot is missed. Better key extraction or user-editable merchant aliases would help.
4. **Assistant coverage.** More phrasings in the rule planner; conversational follow-ups; a plan for savings rate and for
   cash withdrawals.
5. **Partial refunds.** Offer them as suggestions for the user to link rather than ignoring them.
6. **Review UX.** Bulk actions by merchant, undo for approvals, keyboard-friendly review.
7. **Same-day ordering.** Same-day rows are ordered by file position, which can be ambiguous for balance checks; a bank
   sequence number column, when present, would settle it.
8. **Performance.** Analytics loads all transactions into memory. Fine for personal use; revisit if a database grows to
   hundreds of thousands of rows.
9. **Alert text storage.** Alert details (amounts) are stored locally to explain a comparison and are included in backups
   and in a wipe; treat them as sensitive.
10. **Stronger parser containment.** Isolation limits crashes and memory, not exploits. A low-privilege account or an OS
    sandbox would contain a real exploit.
11. **Automate the dynamic offline check** (record connections during a scripted import, categorize and query run).
12. **Streaming backups** so very large databases are not encrypted in memory.

---

## 5. Testing

### How to run
```
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest --cov=bank_analyzer
```

### Latest result (after the Docker and statement-understanding work)
ruff clean, mypy strict clean (64 source files). **Windows:** 1427 passed, 3 skipped. **Docker (Linux, Python 3.11, real
Streamlit and Tesseract):** 1411 passed, 25 skipped (the stub-harness tests skip when real Streamlit is present). Coverage
was last measured after Phase 8 (about 97%) and has not been re-measured.
`python scripts/verify_offline.py` passes (0 remote references, configuration local-only).

### Statement understanding (after Phase 8)
Built: `importer/layout.py` (a statement's layout, applied and then **checked against the document's own arithmetic**:
running balance, closing balance, footer totals), `importer/heuristics.py` (rules read standard layouts with no model),
`ai/statement_reader.py` (local model fallback; validated JSON; one retry; model output repaired by rules where a fact
can be established, e.g. number separators), a word-position PDF table extractor (columns from blank gutters, refuses
when ambiguous), PDF header/footer text carried to the layout step, year-less dates (year found in the document text),
B/F and C/F rows set aside. Import Center has an opt-in "Analyze the layout" step: a reading is offered for use only if
the checks agree.
Verified for real (invented statement in the layout of a real bank PDF, real PDF reader): rules read it in 0.1 s with
all four checks passing; with unfamiliar column headings, the real `llama3.2:3b` read the layout and all checks passed
(about 3 minutes, one retry). Earlier, an unrepaired 3B answer had wrong separators and no balance column, and the checks
correctly rejected it. `qwen2.5:7b` took about 8 minutes per call on this machine (4-core CPU, no usable GPU) and
once returned an unusable layout; it has not been re-tested since the prompt was shortened.
**Not verified:** any real bank statement (only the screenshot layout, rebuilt with invented numbers); DR/CR indicator
layouts are not arithmetic-checked; password-protected PDFs are handled (typed password, tested end to end with a generated encrypted PDF) but never with a real bank's encryption; saved import
profiles (IMP-09) not built; the new Import Center step has been exercised only by the stub tests and never clicked in a
browser; multi-page statements with repeated headings are covered by the old extractor only.

### Layout
| Folder | Contents |
|--------|----------|
| `tests/unit/` | Money, dates, models, database, importers, normalization, rules, categorization, analytics, transfers, reconciliation, recurring, anomaly rules, query plans, planner, answer checking, UI support |
| `tests/integration/` | CSV/Excel/PDF import end to end, categorization pipeline, analytics pipeline on a real statement, anomaly service, assistant on a hand-computed database |
| `tests/security/` | Prompt injection, assistant safety, redaction and logging, network guard, CSV formula injection, backup/restore (wrong passphrase, every tampered region, hostile KDF cost), passcode lockout, permission audit (including real `icacls` output), wipe, isolated parsing (timeout, memory cap, forged replies), no remote references |
| `tests/acceptance/` | The doc 01 performance targets (10k-row import, 100k-row queries) and the audit-log content rule |
| `tests/ui/` | Pages against a stand-in Streamlit (runs now); real `AppTest` pages (activates when Streamlit is installed) |
| `tests/fixtures/` | Synthetic CSV, XLSX and PDF files, with builders and expected-value oracles |

### Method
- **Synthetic data only.** No real financial data is in the repository.
- **Hand-computed oracles.** Expected totals are worked out by hand in the test docstrings, not copied from the code's output.
- **Property tests** (hypothesis) for money parsing and plan validation.
- **Fakes for outside systems.** A fake Ollama HTTP server and scripted models stand in for the model runtime.
- **Sabotage checks.** For Phases 2-7, safeguards were deliberately broken one at a time to prove a test notices (6 + 13 + 26 + 18 + 32
  breakages). Survivors were either new tests added or documented as redundant guards.
- **What tests found** (real defects, not just test typos): oversized-CSV-field error leak, footer rows with amounts rejected,
  encrypted-PDF generic error, backup hanging forever when its own connection had an open transaction, offline scanner crashing on an IPv6 URL, a backup integrity check that no test exercised, `REF` pattern deleting words like REFUND, regex-rule cost blowup, frequency-spike window off by
  one, opening balance applied to every import, "3 largest" parsed as 5, forged figure in a description passing the narration check.

### Known weaknesses of the tests
- Passing tests show behaviour against invented statements, not against the variety of real bank formats.
- The stand-in Streamlit checks my code's calls and data shapes, not Streamlit's behaviour or the page appearance.
- The narration check cannot detect a correct number attached to a wrong claim; the evidence table is the backstop.
