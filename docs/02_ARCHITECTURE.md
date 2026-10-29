# 02 — Architecture

## 1. System Context

```
 +----------------------------- User's computer (single trust domain) -----------------------------+
 |                                                                                                  |
 |   Browser (localhost) ---HTTP 127.0.0.1:8501---> Streamlit app (Python process)                  |
 |                                                      |                                           |
 |                                                      |  function calls (no business logic in UI) |
 |                                                      v                                           |
 |        +-------------------- src/bank_analyzer (application core) -------------------+           |
 |        |  importer -> domain -> services -> database       ai -> (read-only queries)   |           |
 |        |                 ^           ^                      |                         |           |
 |        |               security (validation, redaction, logging, temp files)         |           |
 |        +--------------------------------------------------|---------------------------+           |
 |                         |                                  | HTTP 127.0.0.1:11434                 |
 |                         v                                  v                                      |
 |                 SQLite file (data/)                  Ollama (local LLM runtime)                   |
 |                                                                                                  |
 |   Optional: Tesseract OCR (subprocess, local)                                                    |
 +--------------------------------------------------------------------------------------------------+
                      No arrows leave this box at runtime. Install-time downloads only.
```

## 2. Layered Architecture

Dependency rule: arrows point downward only. Upper layers may call lower layers; lower layers never import upper ones.

```
Presentation (Streamlit pages)
        |
        v
Application services (src/bank_analyzer/services, ai)   <-- orchestration + business logic
        |
        +--> Import layer (importer/*)
        +--> Domain (domain/*: models, money, validation)   <-- pure, no I/O
        +--> Persistence (database/*)
        +--> Security (security/*)  <-- cross-cutting, usable by all
```

### A. Presentation Layer (`app.py`, `pages/`)
- Renders data and collects input. Calls service functions. No SQL, no parsing, no calculations.
- Pages: `0_Import_Center`, `1_Transactions`, `2_Analytics`, `3_AI_Assistant`, `4_Settings` (Overview in `app.py`).
- State: Streamlit `st.session_state` holds only UI state (current preview, filters), never long-lived financial data copies beyond a request.
- Rendering untrusted text: use `st.text`/`st.dataframe`; never `unsafe_allow_html=True` with imported strings.

### B. Import Layer (`importer/`)
| Module | Responsibility | Output |
|--------|----------------|--------|
| `csv_importer.py` | Encoding detection (utf-8-sig, utf-16, cp1252 fallback, user override), delimiter sniffing, header-row detection | `RawTable` |
| `excel_importer.py` | openpyxl read-only mode, `data_only=True`, sheet selection, merged-cell handling, no macros/external links | `RawTable` |
| `pdf_importer.py` | pdfplumber table + text-line extraction, page limit, failure reporting | `RawTable` + warnings |
| `ocr_importer.py` | Render pages to images, call local Tesseract, return text with per-word confidence | `RawTable` + confidence map |
| `column_mapper.py` | Heuristic header matching, saved profiles, user overrides | `ColumnMapping` |
| (new) `profiles.py` | Bank-specific import profile CRUD | `ImportProfile` |

`RawTable` = list of rows of strings plus `source_ref` (file hash, sheet/page, row index). Importers never interpret money or dates.

### C. Financial Processing Layer (`services/`, `domain/`)
| Module | Responsibility |
|--------|----------------|
| `domain/money.py` | `Money` value type (minor units + currency), parsing of locale amounts, rounding policy, formatting |
| `domain/models.py` | Pydantic models: `CanonicalTransaction`, `ImportBatch`, `Account`, `Category`, etc. |
| `domain/validation.py` | Row-level validation, ambiguity detection, warning objects |
| `services/normalization.py` | RawTable + ColumnMapping + user choices → canonical transactions; description cleaning |
| `services/duplicates.py` | Candidate generation (within import, against existing data); never deletes |
| `services/reconciliation.py` | Balance continuity checks, statement open/close checks, transfer matching |
| (new) `services/recurring.py` | Recurring candidate detection |

### D. Categorization Layer (`services/categorization.py`, `ai/`)
Cascade: user rules → approved merchant mappings → built-in rules → LLM → `Needs Review` (details in doc 05).

### E. AI Analysis Layer (`ai/`)
| Module | Responsibility |
|--------|----------------|
| `local_llm.py` | Thin Ollama HTTP client: localhost-only URL enforcement, timeouts, JSON-schema `format`, model health check |
| `prompts.py` | Static prompt templates; untrusted data placed in delimited data blocks |
| `query_planner.py` | Question → `QueryPlan` (LLM-assisted with rule-based fallback); validates against allowlist |
| (new) `query_executor.py` | Executes allowlisted plan against read-only connection, returns `AnalysisResult` |
| `response_validator.py` | Validates classification JSON and assistant answers (numbers must come from results) |

### F. Persistence Layer (`database/`)
`connection.py` (rw and ro connections, PRAGMAs), `schema.sql`, `migrations.py`, `repositories.py` (one repository class per aggregate; all SQL parameterized).

### G. Security Layer (`security/`)
`file_security.py` (type/size/magic checks, safe filenames, secure temp dir), `redaction.py` (mask digits/IBANs/names in any text that might reach logs), `logging_config.py` (structured, scrubbed, no message content from user data). (new) `network_guard.py` (optional socket-level outbound block for non-loopback in the app process).

## 3. Key Data Flows

### 3.1 Import flow
```
Select file (Streamlit uploader)
  -> file_security.validate(size, ext, magic)             [reject early]
  -> copy to private temp dir (0600), hash SHA-256
  -> importer.extract() -> RawTable (+warnings)           [original untouched]
  -> column_mapper.suggest() -> ColumnMapping
  -> UI: preview + user confirms/edits mapping, date format, sign convention
  -> normalization.normalize() -> [CanonicalTransaction | ValidationIssue]
  -> duplicates.find_candidates() -> warnings
  -> UI: user confirms
  -> repositories.import_batch(): BEGIN; insert batch; insert txns; COMMIT   [atomic]
  -> categorization.run() for new txns (rules first; LLM optional)
  -> summary; temp files securely removed in finally
```

### 3.2 AI question flow
```
User question (untrusted)
  -> query_planner: classify into plan {name, params}      [LLM suggests; code validates]
  -> allowlist + parameter validation (types, ranges, enum values)  -> reject/clarify on failure
  -> query_executor on READ-ONLY connection -> AnalysisResult (numbers, tx ids, definition, period, currency)
  -> evidence minimizer (aggregates + at most N supporting rows)
  -> local_llm: "explain these results" (instructions fixed; data in delimited block)
  -> response_validator: every figure must exist in AnalysisResult; else fall back to template text
  -> UI: answer + evidence table + transaction references
```

### 3.3 Categorization flow
```
new/uncategorized txn
  -> user rule match? -> set (source=user_rule)
  -> approved merchant mapping? -> set (source=merchant_mapping)
  -> builtin rules? -> set (source=builtin_rule)
  -> LLM available? -> JSON {category, rationale_short, needs_review}
       -> validate enum and schema -> accept as suggestion (source=llm, status=needs_review)
       -> invalid -> status=needs_review, category=NULL
  -> else Needs Review
Rows with review_status in {approved, user_set} are never overwritten.
```

## 4. Key Design Decisions and Rationale

| # | Decision | Rationale | Alternatives considered |
|---|----------|-----------|-------------------------|
| D1 | Money = integer minor units + currency, wrapped in `Money` over `Decimal` | Exact, fast SQL SUM, no float | Decimal strings in SQLite (slower sum, no exponent info) |
| D2 | Signed amount: inflow positive, outflow negative | One column, easy sums | Separate debit/credit columns (kept only as raw import data) |
| D3 | LLM only for classification and prose | Eliminates arithmetic hallucination | LLM-to-SQL (rejected: injection and correctness risk) |
| D4 | Query-plan allowlist with typed parameters | Bounded capability, testable | Free-form tool calling |
| D5 | Read-only second DB connection for assistant | Defense in depth if planner is bypassed | Single rw connection |
| D6 | Ollama over embedding llama.cpp directly | Auditable, simple install, JSON schema output | llama-cpp-python (heavier build on Windows) |
| D7 | Streamlit | Fast local dashboards, matches prompt | FastAPI + React (more code, more attack surface) |
| D8 | SQLite + OS permissions + disk encryption by default | Honest, simple; SQLCipher optional | Mandatory SQLCipher (build pain on Windows) |
| D9 | Dedupe via candidates not constraints | Identical legitimate txns exist | UNIQUE(fingerprint) |
| D10 | Rules before LLM | Deterministic, fast, auditable; LLM is the fallback | LLM-first |
| D11 | Batch rollback via `import_batch_id` and soft state | Traceable | Hard delete only |
| D12 | pydantic for I/O boundaries, dataclass-like frozen models internally | Validation where untrusted | Pydantic everywhere (slower) |
| D13 | Per-currency reporting only | No online FX | Auto-conversion |
| D14 | Settings table plus `config.py` defaults; env vars for deployment | Clear precedence | Config file only |

## 5. Technology Stack

| Concern | Choice | Notes |
|---------|--------|-------|
| Language | Python 3.11 or 3.12 | Pin after real test |
| UI | Streamlit | `.streamlit/config.toml`: address 127.0.0.1, `gatherUsageStats=false`, `enableCORS=true`, `enableXsrfProtection=true`, `headless=true` |
| Tabular | pandas | Presentation and import preview only; core math in Decimal/SQL |
| DB | `sqlite3` stdlib | WAL mode optional; foreign_keys=ON |
| Validation | pydantic v2 | |
| PDF | pdfplumber | Text PDFs |
| Excel | openpyxl | `read_only=True` |
| OCR | Tesseract via `pytesseract` or subprocess; `pdf2image`/`pypdfium2` for rasterization | Optional extra |
| LLM | Ollama REST (`/api/chat`, `/api/tags`) via `httpx` or `requests` limited to loopback | |
| Crypto (backups) | `cryptography` | Optional extra; scrypt KDF + AES-GCM |
| Tests | pytest, pytest-cov, `hypothesis` (optional for money parsing) | |
| Lint | ruff, mypy | Documented in README |

## 6. Directory Structure (adjusted from prompt)

```
bank_statement_analyzer/
├── app.py                          # Overview page
├── pages/
│   ├── 0_Import_Center.py          # NEW: required by section 11
│   ├── 1_Transactions.py
│   ├── 2_Analytics.py
│   ├── 3_AI_Assistant.py
│   └── 4_Settings.py
├── .streamlit/config.toml          # NEW: localhost, no telemetry
├── src/bank_analyzer/
│   ├── config.py
│   ├── importer/   csv_importer.py excel_importer.py pdf_importer.py ocr_importer.py column_mapper.py profiles.py
│   ├── domain/     models.py money.py validation.py
│   ├── database/   connection.py schema.sql migrations.py repositories.py
│   ├── services/   normalization.py categorization.py analytics.py reconciliation.py duplicates.py
│   │               anomaly_detection.py recurring.py transfers.py backup.py exporter.py
│   ├── ai/         local_llm.py prompts.py query_planner.py query_executor.py response_validator.py
│   └── security/   file_security.py redaction.py logging_config.py network_guard.py
├── tests/ unit/ integration/ security/ fixtures/   # synthetic data only
├── scripts/ setup_environment.py verify_offline.py check_model.py make_offline_bundle.py
├── data/ .gitkeep README.md
├── docs/                           # these documents
├── .gitignore  requirements.txt  requirements-dev.txt  pyproject.toml
└── README.md SECURITY.md PRIVACY.md
```

Additions beyond the prompt tree: `0_Import_Center.py`, `.streamlit/config.toml`, `profiles.py`, `recurring.py`,
`transfers.py`, `backup.py`, `exporter.py`, `query_executor.py`, `network_guard.py`, `check_model.py`,
`make_offline_bundle.py`, `pyproject.toml`. Each is justified by a prompt requirement that had no home.

## 7. Interface Contracts (Python signatures, abbreviated)

```python
# domain/money.py
@dataclass(frozen=True)
class Money:
    minor: int            # smallest unit, signed
    currency: str         # ISO 4217, upper-case
    def __add__(self, other: "Money") -> "Money": ...   # raises CurrencyMismatch if currencies differ
    def to_decimal(self) -> Decimal: ...
def parse_amount(text: str, *, decimal_sep: str, thousands_sep: str | None, currency: str) -> Money: ...

# importer/*
class Importer(Protocol):
    def extract(self, path: Path, options: ImportOptions) -> ExtractionResult: ...

# services/normalization.py
def normalize(raw: RawTable, mapping: ColumnMapping, choices: ImportChoices) -> NormalizationResult: ...

# database/repositories.py
class TransactionRepository:
    def import_batch(self, batch: ImportBatch, txns: Sequence[CanonicalTransaction]) -> ImportSummary: ...
    def rollback_batch(self, batch_id: str) -> RollbackSummary: ...
    def query(self, flt: TransactionFilter, page: Page) -> Page[Transaction]: ...

# services/analytics.py   (all pure over sequences of Transaction)
def monthly_cashflow(txns, *, currency: str, exclude_transfers: bool = True) -> MetricResult: ...

# ai/query_planner.py
def plan(question: str, today: date, llm: LocalLLM | None) -> QueryPlan | Clarification: ...

# ai/query_executor.py
def execute(plan: QueryPlan, ro_conn: sqlite3.Connection) -> AnalysisResult: ...

@dataclass(frozen=True)
class MetricResult:
    name: str; definition: str; period: DateRange; currency: str
    value: Money | Decimal | None; unavailable_reason: str | None
    transaction_ids: tuple[str, ...]
```

## 8. Configuration

Precedence: environment variables (`BANKAN_*`) > `settings` table > `config.py` defaults.

| Key | Default | Notes |
|-----|---------|-------|
| `BANKAN_DATA_DIR` | OS user-data dir (`platformdirs`) or `./data` | Created with restricted permissions |
| `BANKAN_DB_PATH` | `<data>/bank.sqlite3` | |
| `BANKAN_OLLAMA_URL` | `http://127.0.0.1:11434` | Non-loopback hosts rejected unless `BANKAN_ALLOW_REMOTE_LLM=1` (documented as unsupported) |
| `BANKAN_MODEL` | configurable | Checked by `check_model.py` |
| `BANKAN_MAX_UPLOAD_MB_CSV_XLSX` / `_PDF` | 25 / 50 | |
| `BANKAN_LLM_BATCH_SIZE` | 20 | |
| `BANKAN_LLM_TIMEOUT_S` | 60 | |
| `BANKAN_BLOCK_OUTBOUND` | 1 | App-level guard |
| `OLLAMA_HOST` | `127.0.0.1:11434` | Set for the Ollama process |
| `OLLAMA_NO_CLOUD` | `1` | If supported by installed version |

## 9. Error Handling and Logging Strategy

- Typed exceptions (`ImportValidationError`, `AmbiguousValueError`, `CurrencyMismatch`, `PlanRejected`, `LLMUnavailable`).
- UI converts exceptions to plain-language messages; no stack traces with data.
- Logging: JSON lines, rotating file in data dir, fields limited to event name, ids, counts, durations, error class.
  Never log descriptions, amounts, balances, file names with personal info, prompts or model output.
- Audit events: `import_created`, `import_rolled_back`, `backup_created`, `restore_done`, `data_deleted`, `category_merged`,
  `rule_changed` — ids and counts only.

## 10. Extensibility

- New bank format = new `ImportProfile` row; no code change.
- New analytic = new function + registry entry + plan schema + tests; not reachable by the assistant until allowlisted.
- Swap LLM model = settings change; swap runtime = implement `LocalLLM` protocol with loopback restriction.
