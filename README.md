# Bank Statement Analyzer

A personal bank statement analyzer that runs **entirely on your own computer**. You import CSV, Excel or
PDF statements, review the categories, see where your money goes, and ask questions in plain language.
No cloud service, no online AI, no telemetry. Your statements never leave the machine.

> **Read this first: honest status.** The core engine (import, categorization, analytics, assistant,
> backups, security controls) is built and tested: about 1,400 automated tests, all on invented data. The
> **web interface has never been run under real Streamlit** because Streamlit could not be installed where it
> was written. It runs against a stand-in in the tests, which checks this project's own code but not
> Streamlit's behaviour or how the pages look. **Expect a few first-run fixes** (see [Install](#install) step 4).
> The local AI has only been tested against fake models, never a real Ollama. Nothing has been tried on real bank
> statements; the detection thresholds are reasoned defaults, not tuned. Full detail:
> [docs/08_ACCEPTANCE_REPORT.md](docs/08_ACCEPTANCE_REPORT.md).

## What it does

| Area | What you get |
|------|--------------|
| Import | CSV, Excel (.xlsx), text PDFs, and scanned PDFs through optional local OCR. You choose date format, separators and which sign means "money out"; nothing is guessed. A preview and explicit confirmation come before anything is saved. Imports can be undone. |
| Categorizing | Your rules, then merchants you approved, then built-in rules, then (optionally) a local AI model that only *suggests*. Nothing the AI suggests is final until you approve it. |
| Analytics | Income, expenses, net cash flow, spending by category, month-over-month change, savings rate, largest transactions, merchants, cash withdrawals, refunds, recurring payments. Every figure shows its definition, period and currency. Transfers between your own accounts are not counted as spending. |
| Alerts | Large amounts, frequency spikes, possible duplicate charges, recurring price rises, new merchants, negative balances. Described neutrally ("this may be legitimate"); you confirm or dismiss. Nothing is ever changed automatically. |
| Assistant | Ask "How much did I spend on groceries last month?". It picks one of 11 fixed question types, computes the answer with the same code as the Analytics page, and shows the transactions behind it. It cannot write to your data or run SQL. |
| Protection | Encrypted backups, CSV export that cannot run spreadsheet formulas, optional passcode, scrubbed logs, file-parsing isolation, a full wipe. |

## What it is not

- Not a bank connection. It never logs in anywhere, never moves money, never asks for credentials.
- Not tax, accounting or financial advice. Categories and alerts can be wrong; that is why you review them.
- Not encrypted at rest by itself. The database file is readable by anyone who can read your files. Use full-disk
  encryption (BitLocker, FileVault, LUKS). Backups *are* encrypted.
- Not a sandbox against malware on your computer, or an administrator.

## Requirements

- Python 3.11 or newer. (Developed and tested on **Windows 11 with Python 3.11**. macOS and Linux commands below
  are written but **untested**.)
- About 1 GB of disk for the app; the AI model is extra (a small model is roughly 2 GB).
- Optional: [Ollama](https://ollama.com) for the local AI, [Tesseract](https://github.com/tesseract-ocr/tesseract)
  for scanned PDFs.

The app is useful **without** the AI: import, rules, analytics, alerts, backups all work with it off.

## Install

All commands are run from the project folder.

**1. Create the environment and install the pinned packages**

| Windows (PowerShell) | macOS / Linux |
|---|---|
| `py -3.11 -m venv .venv` | `python3.11 -m venv .venv` |
| `.\.venv\Scripts\Activate.ps1` | `source .venv/bin/activate` |
| `python -m pip install -r requirements.txt` | `python -m pip install -r requirements.txt` |

**2. Create your data folder and database**

```
python scripts/setup_environment.py
```

This puts your data in a private folder outside the project (Windows: `%LOCALAPPDATA%\BankAnalyzer`;
macOS: `~/Library/Application Support/BankAnalyzer`; Linux: `~/.local/share/bankanalyzer`) and warns if that folder
looks cloud-synced (OneDrive, Dropbox, iCloud) or readable by other accounts. **Do not keep your data in a synced
folder.** Override the location with the `BANKAN_DATA_DIR` environment variable.

**3. Local AI (optional)**

1. Install Ollama and pull a small instruction model, for example `ollama pull llama3.2:3b`.
2. Make Ollama listen only on this computer: set `OLLAMA_HOST=127.0.0.1:11434`. If your Ollama version supports
   it, also set `OLLAMA_NO_CLOUD=1` and turn off automatic updates. **Check your version's documentation:** this
   project cannot verify what a given Ollama version does.
3. Run `python scripts/check_model.py`. It reports whether Ollama is reachable, whether the model is installed,
   whether the model returns valid structured output, and confirms the app works without the model.

To use another model, set `BANKAN_MODEL` (default `llama3.2:3b`). The app refuses a model server that is not on
this computer.

**4. Install the web interface and start it**

Streamlit is **not pinned** in `requirements.txt` because it could not be tested here. Install it, then check it works:

```
python -m pip install streamlit
python -m pytest tests/ui
python scripts/run_app.py
```

Open the address it prints (`127.0.0.1`, port 8501). `scripts/run_app.py` refuses to listen on any address other
than this computer. Stop with Ctrl+C. Once `tests/ui` passes, add the versions of `streamlit` and `pandas` you
installed (`python -m pip freeze`) to `requirements.txt`.

If the page will not start, the likeliest cause is the outbound-connection guard interacting with Streamlit's
server. Set `BANKAN_BLOCK_OUTBOUND=0` and try again, and note it as a problem to fix (the OS firewall below gives
the real protection either way).

**5. Optional: scanned PDFs.** Install Tesseract and make sure `tesseract` is on your PATH (or set
`BANKAN_TESSERACT_CMD`). On Windows the UB Mannheim build or `winget install` works; macOS `brew install
tesseract`; Linux `apt install tesseract-ocr`. OCR output is flagged for review row by row; you must confirm amounts.

**Alternative: Docker (for testing).** `Dockerfile` and `docker-compose.yml` give a Python 3.11 environment with real
Streamlit and Tesseract, plus an Ollama container. Building needs the internet once; running does not.

```
docker compose --profile test run --rm test     # whole test suite, including pages under real Streamlit
docker compose up -d                            # app on http://127.0.0.1:8501 (published to this computer only)
docker compose exec ollama ollama pull llama3.2:3b
docker compose exec app python scripts/check_model.py
```

Notes: inside the container the app listens on `0.0.0.0` (the container's own network) using the launcher's
`--allow-non-loopback` flag; the port is published to the host's `127.0.0.1` only. The app shares Ollama's network, so
the model is still "local" to it. Containers are **not** outbound-blocked by this setup; add a firewall rule if you
need that. Your data lives in the `bank-data` Docker volume, not in the project folder. This setup has not been built
or run yet.

## Using it

1. **Import Center**: create an account (an alias such as "Main checking"; never a full account number), upload a
   file, check the column mapping and the date/amount settings, read the preview, tick the confirmation, import.
   Then categorize.
2. **Transactions**: search and filter; the **Review queue** tab is where you approve or correct categories in bulk.
   Download the filtered list as CSV (formulas in descriptions are defused).
3. **Analytics**: pick a month or range. Warnings tell you when something is uncategorized, a transfer is
   unmatched, or a period is not finished.
4. **AI Assistant**: ask a question; open "How this was calculated" and the evidence table to see what the answer
   rests on.
5. **Settings**: undo an import, add categories and rules, check the database, set a passcode, make an encrypted
   backup, or delete everything.

### Backup, restore, deletion

- **Backup**: Settings → Encrypted backup. Choose a passphrase of at least 12 characters. **If you forget it the
  backup cannot be opened.** The file (`.bankbak`) is encrypted and tamper-evident. Do not store backups in a
  synced folder unless you accept that encrypted copies leave this computer.
- **Restore**: close the app, then `python scripts/restore_backup.py <file> [--overwrite]`. It verifies the backup,
  keeps a copy of the database it replaces, and swaps the new one in. Add `--verify-only` to just check a backup.
- **Deletion**: *Undo import* hides one import's transactions. *Delete all data* overwrites and removes the
  database, logs and passcode. **This is best effort**: copies on SSDs, in synced folders, in earlier backups, in
  swap files or in exports may remain. Only full-disk encryption (destroying the key) gives a hard guarantee.

## Keeping it offline

The app itself makes no outside connections: no telemetry, update checks, fonts, scripts or CDNs, and Streamlit's
usage statistics are off. Three layers back that up, and you should know what each does and does not prove.

1. `python scripts/verify_offline.py` scans this project's files for remote references and checks the
   local-only settings. A pass means **no such reference exists in these files**. It does not prove that nothing
   connects anywhere.
2. An in-app guard blocks any non-local connection from the app's own Python code. It is **defence in depth, not a
   sandbox**: it does not cover Ollama, Tesseract, your browser or other programs.
3. An **operating-system firewall rule** is the only enforceable control. Block outbound traffic for the Python in
   `.venv` and for Ollama. (Temporarily allow Ollama only while downloading a model.)

| | Windows (PowerShell, as administrator) | Linux (untested) | macOS (untested) |
|---|---|---|---|
| Block the app | `New-NetFirewallRule -DisplayName "BankAnalyzer python out" -Direction Outbound -Program "<project>\.venv\Scripts\python.exe" -Action Block` | `sudo iptables -A OUTPUT -m owner --uid-owner <user> ! -o lo -j REJECT` (or the `nftables` equivalent; best used with a dedicated user) | `pf` rules or an application firewall such as LuLu or Little Snitch |
| Block Ollama | same, with `-Program "<path>\ollama.exe"` | same, for the user running Ollama | same |

Local (loopback) traffic between the app, the browser and Ollama is generally not filtered by these rules. To
**see** connections while the app runs (the "dynamic" check), follow section 6 of
[docs/04_SECURITY_AND_THREAT_MODEL.md](docs/04_SECURITY_AND_THREAT_MODEL.md). **This check has not been performed.**

### Installing on a computer with no internet

On an online machine with the **same operating system, CPU architecture and Python version**:

```
python scripts/make_offline_bundle.py build <empty folder>
```

This copies the app, downloads every pinned package as a wheel, and writes `SHA256SUMS`. **The download step has
not been run** in the environment this was written in (it needs the internet); the rest of the script is tested.
Add the Ollama installer, the model files and (optionally) Tesseract yourself; the script does not fetch them.
On the offline machine:

```
python scripts/make_offline_bundle.py verify <bundle folder>
python scripts/make_offline_bundle.py install-command <bundle folder>
```

`verify` compares every file with `SHA256SUMS`; `install-command` prints the `pip install --no-index` command.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `BANKAN_DATA_DIR` | per-OS user data folder | where data lives |
| `BANKAN_DB_PATH` | `<data dir>/bank.sqlite3` | database file |
| `BANKAN_OLLAMA_URL` | `http://127.0.0.1:11434` | model server (must be local) |
| `BANKAN_MODEL` | `llama3.2:3b` | model name |
| `BANKAN_LLM_BATCH_SIZE`, `BANKAN_LLM_TIMEOUT_S` | 20, 60 | batching and timeout |
| `BANKAN_MAX_UPLOAD_MB_CSV_XLSX`, `BANKAN_MAX_UPLOAD_MB_PDF`, `BANKAN_MAX_PDF_PAGES` | 25, 50, 100 | upload limits |
| `BANKAN_BLOCK_OUTBOUND` | on | in-app outbound guard |
| `BANKAN_ALLOW_REMOTE_LLM` | off | **not recommended**: lets prompts go to a non-local model server |

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| "AI offline" badge | Ollama not running, or the model is not installed | Start Ollama; `ollama list`; `python scripts/check_model.py`. Everything except AI suggestions still works. |
| Categorization is slow | Model too big for your CPU | Use a smaller model; lower `BANKAN_LLM_BATCH_SIZE`. |
| Dates look wrong after import | A date like `03/04/2024` fits two formats | Pick the correct format in step 3 and re-check the preview. |
| Amounts are 100 times off | Wrong decimal separator | Set the decimal and thousands separators. |
| Money in and out are swapped | Wrong sign setting (cards often list purchases as positive) | Change "Which amounts are money going out?". |
| PDF yields no rows | A scan, or a layout with no ruled table | Tick the OCR box, or export CSV from your bank. The app never invents rows. |
| "OCR unavailable" | Tesseract missing or not on PATH | Install it; set `BANKAN_TESSERACT_CMD`. |
| "The PDF is password-protected" | The bank encrypted the PDF | Type its password in "PDF password" (Import Center) and click Read file again. It is used once and never saved. |
| "That password does not open the PDF" | Wrong password | Check it; some banks use a date of birth or an ID number. If you cannot open it in a PDF viewer either, ask the bank. |
| "Reading the file took too long" or "parser stopped unexpectedly" | File too large or damaged | Split or re-export it; limits are in the table above. |
| Page will not start | The in-app guard, or Streamlit not installed | See Install step 4. |
| "Port in use" | Another instance is running | Close it, or `python scripts/run_app.py --port 8502`. |
| "database is locked" | Two instances, or a backup running | Close the other instance and retry. |
| "database ... newer than supported" | The data came from a newer version | Upgrade the app, or restore a backup. |
| `verify_offline.py` fails | A remote reference was found | Read the report; remove or replace it. |
| Windows folder-permission warning | The data folder grants access to other accounts | Move it under your user profile (`BANKAN_DATA_DIR`). |
| Passcode forgotten | | Close the app and delete `passcode.json` in the data folder. (It only gates the UI; it does not protect the files.) |

## For developers

```
python -m ruff check .            # lint
python -m mypy src                # strict types
python -m pytest                  # the full suite (about 2 minutes)
python -m pytest --cov=bank_analyzer
```

Tests use only synthetic data and hand-computed expected values. The design is in `docs/`:
[index](docs/00_INDEX_AND_PROMPT_ANALYSIS.md) · [requirements](docs/01_REQUIREMENTS_SPEC.md) ·
[architecture](docs/02_ARCHITECTURE.md) · [data model](docs/03_DATA_MODEL.md) ·
[security](docs/04_SECURITY_AND_THREAT_MODEL.md) · [financial rules and AI](docs/05_FINANCIAL_RULES_AND_AI_DESIGN.md) ·
[test plan](docs/06_IMPLEMENTATION_AND_TEST_PLAN.md) · [status](docs/07_PROJECT_STATUS.md) ·
[acceptance report](docs/08_ACCEPTANCE_REPORT.md). Also [SECURITY.md](SECURITY.md) and [PRIVACY.md](PRIVACY.md).

## License

See [LICENSE](LICENSE).
