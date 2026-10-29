# 07. Project Status: Built, Pending, Improvements and Testing

Last updated after Phase 6 (2026-10-29). This is a status record, not a plan: update it only from evidence (a test that
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

### Roadmap phases not started
| Phase | Scope |
|-------|-------|
| 7 | Security hardening: logging with redaction, network guard and offline verification script, encrypted backup/restore, formula-injection-safe CSV export, parser subprocess isolation with limits, Windows file-permission check, optional start-up passcode |
| 8 | Packaging: README, SECURITY, PRIVACY, troubleshooting, offline install bundle script, final acceptance run against doc 01 |

### Phase 6 follow-through (needed before calling the UI done)
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
| Windows-specific hardening | Not built yet (Phase 7) |

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
9. **Alert text storage.** Alert details (amounts) are stored locally to explain a comparison; Phase 7's redaction and
   backup work should treat them as sensitive.

---

## 5. Testing

### How to run
```
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest --cov=bank_analyzer
```

### Latest result (after Phase 6)
ruff clean, mypy strict clean (48 source files), **1155 passed, 2 skipped**, coverage about **98%** of `src/bank_analyzer`.
Skipped: live Tesseract OCR; real-Streamlit `AppTest` pages (Streamlit not installed).

### Layout
| Folder | Contents |
|--------|----------|
| `tests/unit/` | Money, dates, models, database, importers, normalization, rules, categorization, analytics, transfers, reconciliation, recurring, anomaly rules, query plans, planner, answer checking, UI support |
| `tests/integration/` | CSV/Excel/PDF import end to end, categorization pipeline, analytics pipeline on a real statement, anomaly service, assistant on a hand-computed database |
| `tests/security/` | Prompt injection (descriptions and questions), assistant safety (forged figures, markdown, SQL-looking questions, out-of-catalogue plans) |
| `tests/ui/` | Pages against a stand-in Streamlit (runs now); real `AppTest` pages (activates when Streamlit is installed) |
| `tests/fixtures/` | Synthetic CSV, XLSX and PDF files, with builders and expected-value oracles |

### Method
- **Synthetic data only.** No real financial data is in the repository.
- **Hand-computed oracles.** Expected totals are worked out by hand in the test docstrings, not copied from the code's output.
- **Property tests** (hypothesis) for money parsing and plan validation.
- **Fakes for outside systems.** A fake Ollama HTTP server and scripted models stand in for the model runtime.
- **Sabotage checks.** For Phases 2-6, safeguards were deliberately broken one at a time to prove a test notices (6 + 13 + 26 + 18
  breakages). Survivors were either new tests added or documented as redundant guards.
- **What tests found** (real defects, not just test typos): oversized-CSV-field error leak, footer rows with amounts rejected,
  encrypted-PDF generic error, `REF` pattern deleting words like REFUND, regex-rule cost blowup, frequency-spike window off by
  one, opening balance applied to every import, "3 largest" parsed as 5, forged figure in a description passing the narration check.

### Known weaknesses of the tests
- Passing tests show behaviour against invented statements, not against the variety of real bank formats.
- The stand-in Streamlit checks my code's calls and data shapes, not Streamlit's behaviour or the page appearance.
- The narration check cannot detect a correct number attached to a wrong claim; the evidence table is the backstop.
