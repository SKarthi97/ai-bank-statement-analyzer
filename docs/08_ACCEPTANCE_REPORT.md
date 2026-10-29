# 08. Acceptance Report

Final check of the built system against [01_REQUIREMENTS_SPEC.md](01_REQUIREMENTS_SPEC.md) and the acceptance
criteria in [06_IMPLEMENTATION_AND_TEST_PLAN.md](06_IMPLEMENTATION_AND_TEST_PLAN.md) section 5.
Written at the end of Phase 8 (2026-10-29). Every status below comes from a test that ran, a command whose output was
read, or an inspection of the code; where something was not run, the status says so.

**Legend:** ✔ met and tested here · ◐ partly met (reason given) · ☐ not met · ✔ᴰ met by documentation or inspection
only (no automated test is possible or relevant) · ⚠ the logic is tested here but it has **not run in its real
environment** (real Streamlit, Ollama, Tesseract, or on real statements).

## 1. Result at a glance

| Check | Result (from the final run) |
|---|---|
| Lint (`ruff check`) and format (`ruff format --check`) | clean; 137 files |
| Types (`mypy`, strict) | clean; 61 source files |
| Tests (`pytest`) | **1364 passed, 3 skipped** in about 3 minutes |
| Skipped | live Tesseract OCR; real-Streamlit `AppTest` pages (Streamlit not installed); one POSIX-only permission test (this machine is Windows) |
| Coverage of `src/bank_analyzer` | **97%** (5,179 statements, 162 missed) |
| Dependency conflicts (`pip check`) | none |
| Offline static scan (`scripts/verify_offline.py`) | PASS: 0 remote references, configuration local-only |
| Deliberate safeguard breakages caught by the tests (Phases 2-7) | 6 + 13 + 26 + 18 + 32, all caught |

All **104** requirement IDs in doc 01 are assessed in section 3 (counted from that section's tables):

| Status | Count |
|---|---|
| ✔ met (including ✔ᴰ) | 87 |
| ◐ partly met | 16 (PRV-01, PRV-02, PRV-11, IMP-02, CAT-02, ANO-02, UI-01, UI-02, UI-03, UI-06, UI-07, UI-08, SEC-05, SEC-06, QA-05, QA-06) |
| ☐ not met | 1 (IMP-09, saved import profiles) |

"Met" here includes items that are met only by design or inspection (✔ᴰ) and items whose logic is tested but ⚠ not
yet run in the real environment. It is not a claim that all 87 are proven in real use. The one performance target not
measured (memory) is in section 2.

**Bottom line.** The engine is built and well tested against invented data. The project is **not yet accepted as
finished**: the web interface has never been run under real Streamlit, the local model and OCR have never been run
for real, nothing has been tried on a real bank statement, and the dynamic offline check has not been done. Section 5
lists exactly what to do to close those gaps.

## 2. Non-functional targets (doc 01 section 12)

Measured on the development machine (Windows 11, Python 3.11, synthetic data). Tests: `tests/acceptance/test_nonfunctional.py`.

| Target | Measured | Status |
|---|---|---|
| 10,000-row CSV parsed and previewed in under 10 s | extract 0.07 s + preview 1.0 s + save 0.5 s ≈ **1.6 s**; totals match the generated data exactly | ✔ |
| Transaction filter/sort under 1 s for 100k rows | slowest of five query shapes **0.25 s** (page by date); the others 0.00–0.13 s | ✔ |
| Memory under 1 GB excluding the LLM | **not measured** | ☐ |
| LLM batching configurable, per-request timeout | `BANKAN_LLM_BATCH_SIZE`, `BANKAN_LLM_TIMEOUT_S`; tested in `test_local_llm.py`, `test_categorization.py`. "Cancellable" is not implemented | ◐ |
| CPU-only viable, model choice in settings | model set by `BANKAN_MODEL`; never run on a real model | ⚠ |

Other timings at 100,000 rows (not targets, but you should know): preview 14 s, save 5.5 s, rule categorization 8.7 s, and
**loading the Analytics page's data takes about 4 s** because all transactions are read into memory on each page run.
At 10,000 rows these are 1.0 s, 0.5 s, 0.6 s and 0.3 s.

## 3. Requirements

### 3.1 Privacy and offline (PRV)

| ID | Status | Evidence / gap |
|---|---|---|
| PRV-01 | ◐ | Nothing in the code needs the internet (`scripts/verify_offline.py`, `test_no_remote_references.py`). **The offline run itself was not performed.** |
| PRV-02 | ◐ | By design and inspection: no network code except the loopback model client (`test_prompt_injection.py::test_only_the_loopback_client_touches_the_network`). The dynamic check was not performed. |
| PRV-03 | ✔ | No cloud client, telemetry or financial API anywhere; static scan finds none. |
| PRV-04 | ✔ | Ollama client only (`ai/local_llm.py`). Tested against a fake server; ⚠ never a real Ollama. |
| PRV-05 | ✔ | `.streamlit/config.toml` loopback; `scripts/run_app.py` refuses other addresses (`test_deploy.py`). `netstat`/`ss` check not performed. |
| PRV-06 | ✔ᴰ | No credential fields or code exist. |
| PRV-07 | ✔ᴰ | No bank login, payment or transfer code exists; alerts take no actions (`test_anomaly_service.py`). |
| PRV-08 | ✔ | Static scan: no remote fonts, scripts, images, CDN or update checks. |
| PRV-09 | ✔ | `gatherUsageStats = false`, enforced by `check_config` and `launch_environment`. |
| PRV-10 | ✔ᴰ | Documented in README/SECURITY (`OLLAMA_NO_CLOUD=1` "if your version supports it"). Cannot be verified here. |
| PRV-11 | ◐ | In-app guard tested (`test_network_guard.py`); OS firewall commands documented, untested on Linux/macOS. Guard ⚠ not run inside real Streamlit. |
| PRV-12 | ✔ | Limits are stated in README, SECURITY.md, PRIVACY.md and this report. |

### 3.2 Import (IMP)

| ID | Status | Evidence / gap |
|---|---|---|
| IMP-01 | ✔ | CSV, XLSX, text PDF: `test_csv_importer.py`, `test_excel_importer.py`, `test_pdf_importer.py`, `test_file_import_end_to_end.py`. |
| IMP-02 | ◐ | OCR path and confidence handling tested with fakes; the live-Tesseract test is skipped (not installed). ⚠ |
| IMP-03 | ✔ | Staged flow in `test_import_service.py`; page drives it (`test_pages_stub.py`). ⚠ page under real Streamlit. |
| IMP-04 | ✔ | `test_file_security.py` (private copy, original untouched, cleanup on error). |
| IMP-05 | ✔ | `test_file_security.py` (extension, magic bytes, size, zip-bomb, macros, zip-slip, PDF page limit). |
| IMP-06 | ✔ | `test_column_mapper.py`, `test_normalization.py`; page offers all mappings. |
| IMP-07 | ✔ | `test_dates.py`, `test_money.py`, `test_normalization.py`. |
| IMP-08 | ✔ | Ambiguity rejected or put to the user (`test_dates.py`, `test_money.py`). |
| IMP-09 | ☐ | **Saved bank profiles are not implemented** (priority S). |
| IMP-10 | ✔ | Warnings and duplicate candidates before confirm (`test_import_service.py`). |
| IMP-11 | ✔ | `test_repositories.py::test_atomic_on_failure_leaves_nothing`. |
| IMP-12 | ✔ | Rollback tested (`test_repositories.py`, `test_file_import_end_to_end.py`); history and undo on the Settings page ⚠. |
| IMP-13 | ✔ | Alias plus optional last-4 only; full numbers rejected (`test_repositories.py`). The Import Center does not offer a last-4 field. |
| IMP-14 | ✔ | Source reference stored (`test_normalization.py`, `test_models.py`). |
| IMP-15 | ✔ | Low-confidence rows need acknowledgement (`test_import_service.py`). |
| IMP-16 | ✔ | Re-import warning and override (`test_import_service.py`). |

### 3.3 Financial model (FIN)

| ID | Status | Evidence / gap |
|---|---|---|
| FIN-01 | ✔ | Integer minor units; property tests in `test_money.py`. The Analytics chart converts to `float` **for drawing only**; the table beside it is exact. |
| FIN-02 | ✔ | Sign convention in `test_normalization.py`, `test_reconciliation.py`. |
| FIN-03 | ✔ | Per-currency totals only; mixed currencies ask the user (`test_analytics.py`, `test_assistant.py`). No static-rate feature exists. |
| FIN-04 | ✔ᴰ | No exchange-rate code. |
| FIN-05 | ✔ | All figures come from `services/analytics.py`; model text never becomes a number (`test_answer.py`, `test_assistant_safety.py`). |
| FIN-06 | ✔ | Definition, period, currency, transaction ids on every metric (`test_analytics.py`). |
| FIN-07 | ✔ | Buckets and identity in `test_analytics.py`; transfers/refunds in `test_transfers.py`. |
| FIN-08 | ✔ | Savings-rate guards (`test_analytics.py`). |
| FIN-09 | ✔ | `test_reconciliation.py` (not_available vs mismatch, deposit vs card/loan signs). |
| FIN-10 | ✔ | Nothing is invented: reconciliation reports amounts, not rows (`test_reconciliation.py`). |

### 3.4 Categorization (CAT)

| ID | Status | Evidence / gap |
|---|---|---|
| CAT-01 | ✔ | Cascade order in `test_categorization.py`. |
| CAT-02 | ◐ | Create, rename, merge and disable exist as services (`test_repositories.py`); the **Settings page only creates** categories. |
| CAT-03 | ✔ | `test_response_validator.py`. |
| CAT-04 | ✔ | Minimal context (`test_prompts.py`). |
| CAT-05 | ✔ | `test_prompt_injection.py`. |
| CAT-06 | ✔ | Needs-review state throughout. |
| CAT-07 | ✔ᴰ | The model returns no confidence number and none is displayed. |
| CAT-08 | ✔ | Bulk approve/correct (`test_categorization.py`); review queue page ⚠. |
| CAT-09 | ✔ | `test_categorization.py`, `test_rule_repositories.py`. |
| CAT-10 | ✔ | User choices protected from automation (`test_categorization.py`). |
| CAT-11 | ✔ | Rules-only with the model down (`test_categorization_pipeline.py`). |

### 3.5 Analytics (ANA) and assistant (AST)

| ID | Status | Evidence / gap |
|---|---|---|
| ANA-01 to ANA-03 | ✔ | `test_analytics.py`, `test_analytics_pipeline.py` with hand-computed oracles. |
| ANA-04 | ✔ | `RecurringConfig`, `test_recurring.py`. |
| AST-01 | ✔ | `test_assistant.py`. |
| AST-02 | ✔ | Closed plan catalogue; `test_query_plan.py`, `test_assistant_safety.py`; no `eval`/`exec`/SQL from model text (`test_prompt_injection.py`). |
| AST-03 | ✔ | `test_query_plan.py` (typed, range-checked). |
| AST-04 | ✔ | Read-only enforced (`test_assistant.py::TestReadOnly`, `test_ui_support.py`). |
| AST-05 | ✔ | Clarifications and help text (`test_assistant.py`, `test_query_planner.py`). |
| AST-06 | ✔ | Period, currency and evidence on every answer. |
| AST-07 | ✔ | Data gaps stated; "not causes" wording (`test_assistant.py`). |
| AST-08 | ✔ | Narration number check (`test_answer.py`, `test_assistant_safety.py`). |
| AST-09 | ✔ | Template fallback with the model down (`test_assistant.py`). |

### 3.6 Anomalies (ANO)

| ID | Status | Evidence / gap |
|---|---|---|
| ANO-01, ANO-03 | ✔ | Rules A1–A6 (`test_anomaly_detection.py`). |
| ANO-02 | ◐ | Thresholds and history guards are configurable in code (`AnomalyConfig`); **no settings page for them**, and the defaults are untuned. |
| ANO-04 | ✔ | Dismiss/confirm kept in history (`test_anomaly_service.py`). |
| ANO-05 | ✔ | Wording test. |
| ANO-06 | ✔ | Candidates only (`test_duplicates.py`, `test_assistant.py`). |
| ANO-07 | ✔ | Resolving an alert changes nothing else (`test_anomaly_service.py`). |

### 3.7 User interface (UI) — the weakest area

| ID | Status | Evidence / gap |
|---|---|---|
| UI-01 | ◐ | The Overview shows counts and last month's income, expenses, net and cash. It does **not** show a savings rate, trend chart, category breakdown or recent transactions (the Analytics page has most of these). ⚠ |
| UI-02 | ◐ | Search, filters, pagination, bulk category edit and CSV download exist. No per-row detail view or inline edit, and no merchant filter on the page. ⚠ |
| UI-03 | ◐ | Month comparison, category spend, merchants, recurring list, reconciliation present; no per-category trend view. ⚠ |
| UI-04 | ✔ | Assistant page with evidence. ⚠ under real Streamlit. |
| UI-05 | ✔ | Preview, mapping, issues, duplicates, history, rollback. ⚠ |
| UI-06 | ◐ | Model status, paths, export, delete, offline status and integrity check present; **retention settings absent**. ⚠ |
| UI-07 | ◐ | Undo needs a tick box, wipe needs a typed phrase, restore needs `--overwrite`; empty states exist. Accessibility was not reviewed. |
| UI-08 | ◐ | Pages call services, but some formatting maths lives in pages (percent display, chart values). Not a financial calculation. |
| UI-09 | ✔ | `test_csv_formula_injection.py`. |

**All pages run only against a stand-in Streamlit** (`tests/ui/test_pages_stub.py`: 6 pages, empty and populated
databases, a full import, the passcode gate, the download button). `tests/ui/test_pages_apptest.py` is written but
has **never run**.

### 3.8 Persistence (DB), security (SEC), quality (QA)

| ID | Status | Evidence / gap |
|---|---|---|
| DB-01, DB-04, DB-05 | ✔ | `test_database.py`, `test_repositories.py` (migrations tested on a real v1 database). |
| DB-02 | ✔ | All listed tables exist in `schema.sql`. |
| DB-03 | ✔ | Parameterised SQL; `test_prompt_injection.py::test_no_model_output_is_ever_interpolated_into_sql`. |
| DB-06 | ✔ | `check_integrity` (`test_deploy.py`, including a damaged index and dangling references); Settings button ⚠. |
| DB-07 | ✔ | `test_backup_restore.py` (round trip, wrong passphrase, tampering, corrupt content, hostile parameters). Restore is a CLI script, not a button. |
| DB-08 | ✔ | `test_nonfunctional.py::test_the_audit_log_never_holds_statement_content`. |
| SEC-01, SEC-02, SEC-03 | ✔ | `test_file_security.py`. |
| SEC-04 | ✔ | `test_redaction_and_logging.py`. |
| SEC-05 | ◐ | POSIX modes set; Windows ACL is **audited, not set** (`test_passcode_permissions_wipe.py`, tested against real `icacls` output). |
| SEC-06 | ◐ | Every package pinned and `pip check` clean; review procedure documented in SECURITY.md. **`pip-audit` was not run and hashes were not generated** (both need the internet). |
| SEC-07 | ✔ | docs/04 and SECURITY.md. |
| SEC-08 | ✔ | The database is stated to be unencrypted (README, SECURITY, PRIVACY). |
| SEC-09 | ✔ | Untrusted text shown with `st.text`/`st.dataframe` after `safe_cell`; `escape_markdown` available (`test_ui_support.py`, `test_answer.py`). |
| QA-01 | ✔ | `ruff` and strict `mypy` clean. |
| QA-02 | ✔ | Fixtures are synthetic; generated by scripts in `tests/fixtures`. |
| QA-03, QA-04 | ✔ | Unit, integration (fake Ollama), security, offline and AI-down tests. |
| QA-05 | ◐ | Full pins and an offline bundle script. **The wheel download step has not been run** (needs internet); Streamlit is not pinned. |
| QA-06 | ◐ | README covers Windows, macOS and Linux; **only Windows was exercised**. |
| QA-07 | ✔ | `scripts/check_model.py`, `scripts/verify_offline.py` (tested). |
| QA-08 | ✔ | docs/06 checklist and docs/07. |
| QA-09 | ✔ | Numbers in these docs come from runs; see section 1. |

## 4. Acceptance criteria (doc 06 section 5)

| Criterion | Status | Evidence |
|---|---|---|
| Import a synthetic statement and preview it | ✔ ⚠ | `test_file_import_end_to_end.py`; the page ran end to end only against the stand-in |
| Correct column mappings | ✔ | `test_column_mapper.py` |
| Review and categorize | ✔ ⚠ | `test_categorization*.py`; review page unverified under real Streamlit |
| Monthly totals match independent results | ✔ | `*.expected.json` oracles, `test_analytics_pipeline.py` |
| Duplicate detection never silently deletes | ✔ | row counts asserted unchanged (`test_assistant.py`, `test_import_service.py`) |
| Assistant answers approved questions | ✔ ⚠ | `test_assistant.py` with fake models; never a real model |
| Invalid model responses cannot corrupt data | ✔ | `test_response_validator.py`, `test_categorization.py`, `test_assistant_safety.py` |
| Useful without AI | ✔ | AI-down tests across import, rules, analytics, assistant |
| No external services in normal operation | ◐ | static scan passes; **dynamic check not performed** |
| Not exposed to the network | ◐ | config and launcher tested; `netstat`/`ss` check not performed |
| Export, backup, restore, deletion | ✔ ⚠ | services tested; Settings/Transactions pages unverified; restore is CLI |
| Tests cover core finance and security | ✔ | coverage in section 1 |
| Offline operation tested and documented | ◐ | documented; the dynamic test was not run |
| Limitations disclosed | ✔ | README, SECURITY.md, PRIVACY.md |

## 5. What must still be done before relying on it

In this order:

1. **Install Streamlit and run `python -m pytest tests/ui`.** Fix anything the real runner finds, then pin the working
   `streamlit` and `pandas` versions. Open each page by hand (`python scripts/run_app.py`). If the app will not start,
   try `BANKAN_BLOCK_OUTBOUND=0` first (the in-app guard is the likeliest cause) and report it.
2. **Try it on a real statement you are comfortable with**, then review every category and alert. Expect to adjust
   defaults (alert thresholds, built-in rules, the refund window). Nothing has been tuned on real data.
3. **Install Ollama and Tesseract**, run `python scripts/check_model.py`, and import a scanned PDF.
4. **Do the dynamic offline check** (docs/04 section 6) and add the firewall rules from the README.
5. On an online machine, run `pip-audit`, generate hashes, and try `scripts/make_offline_bundle.py build`.
6. Try the macOS/Linux commands if you use those systems.

## 6. Known limitations that are by design

Partial refunds are not linked (a guess would hide real spending). Transfers are linked automatically only when the
match is unique and shows transfer evidence. Follow-up questions are not understood by the assistant. The database is
not encrypted. Wiping is best effort. Alert thresholds are untuned defaults. Parser isolation limits crashes, not
exploits.
