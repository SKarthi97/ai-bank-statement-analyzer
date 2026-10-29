# Privacy

**Short version:** everything stays on your computer. The app does not send your statements, transactions,
questions or answers anywhere. There is no account, no sign-up, no analytics and no update check.

This page describes what is stored, where, what is shared (nothing, by design), and how to remove it. For how the
claim is checked and its limits, see [SECURITY.md](SECURITY.md).

## What the app stores

| Data | Where | Notes |
|---|---|---|
| Transactions you import (dates, descriptions, amounts, currencies, running balances if present) | The database file `bank.sqlite3` in your data folder | The original description is kept verbatim; a cleaned copy is used for matching. |
| Account aliases (for example "Main checking"), currencies, optional last-4 | Same database | **Full account numbers are not stored.** Do not put one in an alias. |
| Categories, rules, merchant mappings, your approvals | Same database | |
| Alerts and recurring-payment records | Same database | Alert text includes the amounts needed to explain a comparison. |
| Audit events | Same database | Event names, ids and counts only: never descriptions or amounts. |
| Logs | `logs/` in your data folder | Scrubbed: amounts, long digit runs, IBANs, e-mail addresses and URL queries are removed, and exception messages are not written. Free text cannot be reliably recognised, so the rule is that nothing logs statement content. |
| Passcode (if you set one) | `passcode.json` | Only a salted scrypt hash, never the passcode. |
| Backups you create | `backups/` in your data folder | Encrypted with your passphrase, which is never stored. |
| Exports you download | Wherever your browser saves them | **Not encrypted.** CSV files are plain text. |
| Uploaded files | A private temporary folder during import | Deleted when the import step ends, even on error. Your original file is never modified. |

The default data folder is outside the project and outside common cloud-sync folders. The app warns if the folder you
chose looks synced.

## What leaves your computer

**Nothing, by design.** The app has no telemetry, crash reporting, update check, remote fonts, scripts or images, and
Streamlit's usage statistics are switched off. It never asks for or stores banking credentials, never connects to a bank
and never moves money.

Be aware of what is **outside** the app's control:

- **The local AI (Ollama).** The app sends it only short cleaned descriptions and a money-in/money-out flag when
  categorizing (no amounts, dates, balances or account names). For the assistant it sends, only when its fixed phrase
  rules do not understand a question, your question text and your category names, and, to rephrase an answer, the
  computed figures and up to 20 supporting rows (dates, cleaned descriptions, amounts). This goes to a model server **on your own computer**; the app refuses to use one that is not.
  What Ollama itself does (downloads, updates, cloud features) depends on your version and settings. Configure it to
  stay local and check its documentation; this project cannot verify it.
- **Your browser** shows the pages and may keep its own history or cache.
- **Tesseract** (optional OCR) runs locally on page images from your PDF.
- **Cloud-synced folders and backup services** copy anything saved inside them. Keep your data folder and backups out
  of them unless you accept that.
- **Other programs and the operating system** can see your files. The database is **not encrypted by the app**; use
  full-disk encryption.

## Why the AI is limited

To keep your data private and your records correct, the AI never receives more than it needs, never writes to your data
(suggestions wait for your approval), and cannot run queries of its own. If it is switched off, the app still works.

## Your controls

- **See what is stored:** the Transactions page, and the data folder shown in Settings.
- **Export:** Transactions → download CSV (formula-safe, unencrypted), or an encrypted backup from Settings.
- **Undo an import:** Settings → Undo an import (hides its transactions from every total; the rows are kept for
  traceability until you delete everything).
- **Delete everything:** Settings → Delete all data (type the confirmation phrase). Overwrites and removes the database,
  logs and passcode; backups only if you tick the box.
  **This is best effort.** Copies may remain on SSDs, in synced folders, in earlier backups, in swap files, in exports
  you saved, and in the browser. Only full-disk encryption (destroying the key) or physical destruction guarantees
  removal.
- **Stop it using the AI at all:** do not install Ollama, or leave it stopped. The app shows "AI offline" and carries on.

## Other people's data

Statements can contain other people's names and payments. The data stays on your computer, but you are responsible for
what you import and for who can access your computer.
