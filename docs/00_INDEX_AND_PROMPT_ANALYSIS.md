# Offline AI Bank Statement Analyzer — Documentation Index & Prompt Analysis

Source: `OFFLINE_BANK_STATEMENT_ANALYZER_PROMPT.md` (the "Master Engineering Prompt").
Status: design documentation only. No application code has been written yet.

## 1. Document Set

| # | Document | Purpose |
|---|----------|---------|
| 00 | This file | Summary, analysis of the prompt, gaps, assumptions, glossary |
| 01 | [01_REQUIREMENTS_SPEC.md](01_REQUIREMENTS_SPEC.md) | Numbered functional and non-functional requirements |
| 02 | [02_ARCHITECTURE.md](02_ARCHITECTURE.md) | Layers, interfaces, data flows, tech decisions, directory layout |
| 03 | [03_DATA_MODEL.md](03_DATA_MODEL.md) | Canonical models, SQLite schema, migrations, integrity rules |
| 04 | [04_SECURITY_AND_THREAT_MODEL.md](04_SECURITY_AND_THREAT_MODEL.md) | Trust boundaries, threats, controls, residual risks |
| 05 | [05_FINANCIAL_RULES_AND_AI_DESIGN.md](05_FINANCIAL_RULES_AND_AI_DESIGN.md) | Accounting definitions, categorization, analytics, assistant query plans |
| 06 | [06_IMPLEMENTATION_AND_TEST_PLAN.md](06_IMPLEMENTATION_AND_TEST_PLAN.md) | Phases, test matrix, acceptance criteria, feature checklist |

## 2. What the Prompt Asks For (One-Paragraph Summary)

A single-user, local-first desktop-class web app (Streamlit on localhost) that imports CSV, XLSX, text PDF and
(optionally) scanned PDF bank statements; normalizes them into a canonical transaction model stored in SQLite;
categorizes them with a hybrid of deterministic rules plus a local LLM (Ollama); computes all finance metrics with
deterministic `Decimal` code; flags explainable anomalies and duplicates; and offers a natural-language assistant that
can only call an allowlisted, read-only query interface. Privacy is the top priority: no cloud, no telemetry, localhost
binding, and honest documentation of what cannot be guaranteed.

## 3. Prompt Structure Analysis

| Prompt section | Nature | Maps to doc |
|----------------|--------|-------------|
| 1 Role & objective | Framing, deliverable expectation (runnable project, not outline) | 00 |
| 2 Non-negotiables | Hard constraints (offline, local LLM, deterministic math, traceability, human review) | 01, 04 |
| 3 Users & environment | Win/macOS/Linux, Python 3.11+, Streamlit, pandas, SQLite, Ollama, Pydantic, pdfplumber, openpyxl, Tesseract, pytest | 02 |
| 4 Architecture | Seven layers A–G | 02 |
| 5 Directory structure | Prescribed tree | 02 |
| 6 Import & normalization | 10-step staged workflow, canonical model | 02, 03 |
| 7 Categorization | Rule cascade, AI constraints, categories | 05 |
| 8 Analytics | 15+ metrics, currency and transfer rules | 05 |
| 9 AI assistant | Plan → deterministic query → grounded explanation | 05 |
| 10 Anomalies/duplicates | Rule-based, explainable, never auto-delete | 05 |
| 11 Dashboard | Six UI areas | 02 |
| 12 Security | ~20 controls | 04 |
| 13 Database | Tables, constraints, rollback | 03 |
| 14 Testing | ~23 test areas | 06 |
| 15 Installation | Per-OS instructions, offline bundle | 06 |
| 16 Workflow | 8 phases | 06 |
| 17 Coding standards | Typing, Decimal, logging, docstrings | 02 |
| 18 Deliverables | Docs, tests, scripts, checklist | 06 |
| 19 Acceptance | 13 criteria | 06 |
| 20 First response | Present design, then build Phases 1–2 | 02, 06 |

## 4. Key Design Principles Extracted

1. **Privacy over capability.** If AI complexity conflicts with privacy or correctness, drop the AI feature.
2. **The LLM never computes and never acts.** It classifies text into an allowed enum and phrases explanations of
   numbers that Python already computed.
3. **Everything is traceable.** Any number on screen resolves to a list of transaction IDs.
4. **Fail useful.** With the LLM or OCR missing, import, search, rules, analytics and reports still work.
5. **No silent guessing.** Ambiguous dates, signs and categories go to the user.
6. **Never delete automatically.** Duplicates and anomalies are candidates only.
7. **Honest claims.** Document limits (SQLite not encrypted by default, firewall is OS-level, etc.).

## 5. Gaps, Ambiguities and Risks Found in the Prompt

| # | Observation | Impact | Assumption / recommendation used in these docs |
|---|-------------|--------|-----------------------------------------------|
| G1 | "Run entirely offline" vs. Ollama model download and pip install | Install needs internet once | Distinguish *install-time* from *runtime* network; provide offline bundle procedure (doc 06) |
| G2 | `OLLAMA_NO_CLOUD=1` cited "where applicable" | Variable may not exist in every Ollama version | Verify against installed Ollama version at setup; do not rely on it alone; add firewall rules |
| G3 | Section 5 lists no `pages/` for Import Center, but Section 11 requires an Import Center | UI mismatch | Add `pages/0_Import_Center.py` (permitted: "may adjust structure") |
| G4 | No `services/transfers.py` although transfer matching is required | Missing module | Transfer matching lives in `reconciliation.py` or a new `transfers.py` |
| G5 | No repository/module for recurring detection and alert history | Missing module | Add `services/recurring.py`; alerts persisted via `repositories.py` |
| G6 | "Multiple currencies" but no FX conversion allowed online | No single grand total | Per-currency totals only; optional user-entered static FX table, off by default |
| G7 | "Encrypted backups" without chosen tool | Ambiguous | Use standard-library-compatible approach: `cryptography` (Fernet/AES-GCM with scrypt-derived key) or document OS/`age`/7-Zip AES; pick one in Phase 7 |
| G8 | "Database-level encryption" optional | Dependency choice | Default: OS full-disk encryption; optional SQLCipher documented as an add-on, not default |
| G9 | Fingerprint dedupe vs. legitimately identical transactions | Risk of dropping real data | Fingerprint plus occurrence index plus import-batch history; duplicates are warnings, user decides |
| G10 | Savings rate "when meaningful" | Undefined | Define: requires income > 0 in a single currency over a full calendar period, and transfers excluded (doc 05) |
| G11 | Credit-card statements have opposite sign conventions | Reconciliation errors | Per-account `statement_type` (`deposit`, `credit_card`, `loan`) controls sign rules |
| G12 | OCR confidence handling unspecified | Silent bad digits | Low-confidence OCR rows forced to review; amounts never auto-accepted |
| G13 | Prompt asks for ~60 source files plus docs in one go | Too large for one pass | Follow the prompt's own phased workflow and file manifest |
| G14 | Streamlit runs a local web server; browsers can reach it | Other local processes / browser pages could connect | Bind `127.0.0.1`, disable CORS relaxations, keep XSRF protection on; document that localhost is not a security boundary against other local users or malware |
| G15 | Streamlit telemetry | Violates "no telemetry" | Set `browser.gatherUsageStats=false` in `.streamlit/config.toml` |
| G16 | Streamlit and other libraries may load remote assets | Violates "no CDN" | Verify in offline test; vendor assets or avoid components that fetch remote resources |
| G17 | "Never claim tests passed unless executed" | Process rule | Test report must record real command output |

## 6. Assumptions (Stated Because the Prompt Says to Proceed)

- Python 3.11 or 3.12; exact pins decided and recorded at build time after a real install.
- One user, one machine, no multi-user auth. Protection relies on OS accounts and disk encryption.
- Default currency from config; every transaction carries an explicit ISO 4217 code.
- Money is stored as integer minor units plus currency code; currencies with non-2 decimals (JPY=0, KWD=3) handled
  through an exponent lookup.
- Local LLM default: a small instruct model (for example a 3B–8B class model) selectable in settings; no GPU needed.
- Dates stored as ISO `YYYY-MM-DD` without time zones (statement dates are civil dates); timestamps for
  created/updated stored as timezone-aware UTC.

## 7. Glossary

| Term | Meaning |
|------|---------|
| Canonical transaction | Normalized, validated representation of one statement line |
| Import batch | One file import event; the unit of rollback |
| Query plan | Validated structured object (name + typed parameters) selecting an allowlisted analytic |
| Review queue | Transactions or alerts awaiting human confirmation |
| Minor units | Integer smallest currency unit (cents) used for exact arithmetic |
| Trust boundary | Line across which data must be validated (file → parser, DB → LLM, LLM → app) |
| Residual risk | Risk remaining after controls, documented rather than hidden |
