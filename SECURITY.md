# Security

This is a personal, single-user tool that handles sensitive financial data. This page says what it protects,
how, and, just as important, **what it does not protect**. The full threat model is in
[docs/04_SECURITY_AND_THREAT_MODEL.md](docs/04_SECURITY_AND_THREAT_MODEL.md).

## Reporting a problem

If you find a security problem, tell the maintainer privately (not in a public issue) and include the steps to
reproduce it using **invented data only**. Do not send real statements to anyone.

## What is protected, and how

| Risk | Control | Limits |
|---|---|---|
| Data leaving the computer | No cloud services, telemetry, update checks, remote fonts or scripts. Local model server must be on this computer (loopback only, proxies ignored, redirects refused). An in-app guard blocks non-local connections from the app's Python code. | The guard is **not a sandbox**; it does not cover Ollama, Tesseract, your browser or native code. Only an OS firewall rule is enforceable. |
| Exposure to other computers | The UI binds to `127.0.0.1`; `scripts/run_app.py` refuses any other address. | Other accounts on the same computer, and web pages in your browser that can reach `localhost`, are not fully excluded. Streamlit's XSRF/origin checks stay on; an optional passcode adds a gate. |
| Hostile statement files | Extension allow-list, size and page limits, magic-byte checks, zip-bomb/macro/zip-slip rejection, PDF active-content warning, private temp copy deleted afterwards. Parsing runs in a **child process** with a timeout and memory cap. | The child has the same user rights and network access as the app. This contains crashes and runaway memory, **not a deliberate exploit** of a parser library. |
| The AI doing something it should not | The model has no tools. It only returns JSON that is validated; categories it suggests stay "needs review" until you approve. The assistant's question becomes one of 11 fixed plans, runs on a **read-only** database connection, and its wording is checked against the computed numbers. Statement text reaches the model only inside a JSON data block. | A model can still suggest a wrong category or phrase an answer badly; the evidence table is always shown so you can check. |
| Formula injection in exports | Text cells that could run as a formula get an apostrophe prefix. | The apostrophe may be visible in some tools. |
| Data in logs | Logs are scrubbed (amounts, long digit runs, IBANs, e-mail, URL queries removed); exception messages are never written; no log record reaches another handler. The audit trail stores event names, ids and counts only. | Redaction is pattern-based and cannot recognise a free-text description. The rule is: log events and counts, not data. |
| Stolen or copied backups | AES-256-GCM, scrypt key from your passphrase (12+ characters), authenticated header, integrity check on create and restore. | A weak passphrase can be guessed offline; a forgotten one cannot be recovered. |
| Local access to the database file | Data folder and files are owner-only on POSIX; on Windows the ACL is **audited** (reported, not changed). Default location is outside synced folders, with a warning if it is inside one. | **The database is not encrypted by the app.** Use full-disk encryption. |
| Dependency compromise | All packages pinned to tested versions, wheels-only offline bundle with SHA-256 checksums. | Hashes are not generated (see below); pins reduce but do not remove supply-chain risk. |

## What this does NOT protect against

1. **Malware, or an administrator, on your computer.** Anything that can run as you can read everything you can.
2. **A lost or stolen device without full-disk encryption.** The database is readable.
3. **The app's own database file is not encrypted.** Only backups and exports of your choosing are.
4. **Parser exploits.** Zero-days in PDF, Excel or OCR libraries could still be triggered by a hostile file. Isolation
   limits crashes, not exploits. Only import files you trust.
5. **Anything outside this app's code that touches the network**: Ollama, Tesseract, your browser, other programs.
6. **Deleted data.** Wiping is best effort; SSD wear-levelling, cloud sync, earlier backups, OS caches and swap
   may retain copies.
7. **The passcode is not encryption.** It keeps casual visitors out of the UI; anyone with file access can remove it.
8. **A wrong answer.** Categories, alerts and assistant answers can be mistaken. They are prompts to look, not facts.
9. **Browser-to-localhost attacks** cannot be completely eliminated for any local web app.
10. **Offline tests cannot prove the absence of network access in every code path.** `scripts/verify_offline.py` proves
    that no remote reference exists in this project's own files, nothing more.

## Hardening checklist for you

- [ ] Turn on full-disk encryption (BitLocker / FileVault / LUKS).
- [ ] Keep the data folder outside OneDrive, Dropbox, iCloud and similar. `setup_environment.py` warns you.
- [ ] Add OS firewall rules blocking outbound traffic for the app's Python and for Ollama (README, "Keeping it offline").
- [ ] Set Ollama to listen on `127.0.0.1` only and disable its cloud features and automatic updates if your version
      supports it. **Verify against your version's documentation**; this project cannot.
- [ ] Use a dedicated operating-system account if others use the computer.
- [ ] Set a start-up passcode if others can reach your screen or local ports.
- [ ] Make encrypted backups with a strong passphrase, and test a restore (`--verify-only`).
- [ ] Run `python scripts/verify_offline.py` after any change to the code.

## Vulnerability review procedure (dependencies)

Dependencies are pinned in `requirements.txt` (all direct and transitive packages). To review them:

1. On a machine with internet access, in a throw-away environment: `python -m pip install pip-audit`, then
   `python -m pip_audit -r requirements.txt`. Read each finding; this step is online and is **not** part of normal use.
2. For a package that parses untrusted files (`pdfplumber`, `pdfminer.six`, `pypdfium2`, `pillow`, `openpyxl`) or handles
   secrets (`cryptography`), also read its release notes for security fixes.
3. Update pins one package at a time, run `python -m pytest` and `python scripts/verify_offline.py`, then record the new
   versions. To lock hashes, use `pip-compile --generate-hashes` and install with `--require-hashes`.
4. Repeat at least when you update the app and whenever a security advisory affects a pinned package.

**Not done yet:** hash-pinning, and a run of `pip-audit` (both need the internet, which the build environment lacked).

## Known gaps in what has been verified

- The web UI, the in-app network guard and the passcode gate have **never run under real Streamlit**.
- The local-model code has only been tested against fake servers and fake models, not a real Ollama.
- OCR has never been run against a real Tesseract install.
- The Linux/macOS memory limit for the parser child process is written but untested; the Windows one is tested.
- The dynamic offline check (watching real connections while the app runs) has not been performed.
- Everything has been tested only on invented data, never on real bank statements.
