# 04 — Security, Privacy and Threat Model

## 1. Assets

| Asset | Sensitivity |
|-------|-------------|
| Imported statement files | High |
| SQLite database (+ WAL/SHM) | High |
| Descriptions, merchants, amounts, balances | High |
| Account aliases / last-4 | Medium |
| Categorization rules, settings | Low–Medium |
| Logs, temp files, backups, exports | High if they contain data (must not by default for logs) |
| LLM prompts and outputs | High (contain transaction text) |

## 2. Trust Boundaries

```
[Untrusted] statement file  ==TB1==>  parsers
[Untrusted] transaction text ==TB2==> LLM prompts / UI rendering / CSV export
[Untrusted] user question    ==TB3==> query planner
[Semi-trusted] LLM output    ==TB4==> application (must be schema-validated)
[Trusted] application <-> SQLite file (protected only by OS permissions)
[Boundary] application <-> Ollama over loopback (other local processes can also reach loopback)
[Boundary] machine <-> network (default deny outbound for app; firewall guidance)
[Boundary] browser <-> Streamlit on localhost (other local users/processes/pages can attempt access)
```

## 3. Threat Model (STRIDE-oriented)

| ID | Threat | Vector | Controls | Residual |
|----|--------|--------|----------|----------|
| T1 | Malicious/malformed file exploits parser | Crafted PDF/XLSX/CSV | Size/page/row limits, magic-byte check, openpyxl read-only without macros/external links, zip-ratio check, parser timeouts, run parsing in a subprocess with timeout (Phase 7), dependency pinning | Zero-days in pdfplumber/pdfminer/openpyxl/Tesseract |
| T2 | Zip bomb / decompression DoS | XLSX | Uncompressed-size cap, entry-count cap | Low |
| T3 | Path traversal / unsafe filename | Upload name | Never use user filename for paths; generated temp names; sanitize display name | Low |
| T4 | Formula injection in exports | Descriptions starting `= + - @ \t \r` | Prefix with `'` on CSV/XLSX export; test | User opening in other tools with unusual rules |
| T5 | Prompt injection via description | "Ignore previous instructions…" | Fixed system prompt, data in delimited block, JSON-schema output, enum validation, no tools exposed to LLM, outputs never executed | Wrong category suggestions still possible → human review |
| T6 | LLM-driven arbitrary SQL/command/file/network access | Assistant | LLM has no tool/exec channel; plan allowlist; RO DB connection; typed params; no `eval`/`exec`/`subprocess` on model text | Planner bugs → covered by tests |
| T7 | Data exfiltration through app | Telemetry, CDN, update checks, remote LLM | No such code; Streamlit stats off; URL allowlist (loopback only); app-level socket guard; offline verification script; firewall guidance | App-level guard is not a sandbox; OS firewall required for assurance |
| T8 | Ollama contacts internet | Model pulls, cloud features | `OLLAMA_NO_CLOUD=1` where supported; models pre-pulled; firewall rule for `ollama` binary outbound; verification procedure | Ollama is third-party; behavior varies by version |
| T9 | Other local process/user reads DB | File permissions | Data dir 0700 / user-only ACL; DB 0600; warn on world-readable | Same-user malware, admin/root, physical access |
| T10 | Browser-based attack on localhost service (DNS rebinding, malicious web page calling localhost) | Visit hostile site while app runs | Bind 127.0.0.1, keep XSRF protection and Streamlit origin checks on, no unauthenticated state-changing GET endpoints, document risk, optional launch token/passcode | Residual browser-side risk; localhost ≠ authenticated |
| T11 | Disk theft / lost laptop | Physical | Full-disk encryption guidance (BitLocker, FileVault, LUKS); encrypted backups | Without FDE, data is readable |
| T12 | Sensitive data in logs/crash dumps | Exceptions | Structured scrubbed logs, no payload logging, redaction util, tests scanning logs | OS crash dumps / swap |
| T13 | Temp file leakage | Uploads | Private temp dir, 0600, deletion in `finally`, startup sweep | SSD remnants |
| T14 | Cloud sync leakage (OneDrive etc.) | DB/exports stored in synced folder | Default data dir outside sync folders; Settings warning | User override |
| T15 | Backup theft | Backup files | AES-GCM encrypted with scrypt-derived key, passphrase never stored | Weak passphrase |
| T16 | Supply-chain compromise | Dependencies | Pinned versions with hashes (`pip install --require-hashes`), minimal deps, `pip-audit` at build time (online step), offline wheel bundle | Compromised upstream at download time |
| T17 | Database corruption / bad AI write | Crash, invalid model output | Transactions, constraints, integrity check, AI cannot write categories without validation and human review gate, backups before migration | Low |
| T18 | Misleading financial conclusions | Bugs, LLM prose | Deterministic calcs, answer-number validator, evidence display, definitions on every metric | User misinterpretation |
| T19 | Over-reliance on "fraud" alerts | UX | Neutral wording, evidence, dismiss/confirm | — |
| T20 | Credential collection | Feature creep | Explicit non-goal, code review checklist | — |

## 4. Control Catalogue (implementation map)

| Control | Module | Test |
|---------|--------|------|
| File type/size/magic validation | `security/file_security.py` | `tests/security/test_file_validation.py` |
| Safe filenames/paths | `file_security.py` | `test_path_traversal.py` |
| Secure temp handling | `file_security.py` | `test_temp_cleanup.py` |
| Redaction | `security/redaction.py` | `test_redaction.py` |
| Scrubbed logging | `security/logging_config.py` | `test_logs_contain_no_financial_data.py` |
| Loopback-only LLM URL | `ai/local_llm.py` | `test_llm_url_enforcement.py` |
| App-level outbound guard | `security/network_guard.py` | `test_network_guard.py` |
| Formula-injection-safe export | `services/exporter.py` | `test_csv_formula_injection.py` |
| Read-only assistant DB | `database/connection.py` | `test_readonly_connection.py` |
| Plan allowlist | `ai/query_planner.py`, `query_executor.py` | `test_unauthorized_queries.py` |
| Prompt-injection resilience | `ai/prompts.py`, `response_validator.py` | `test_prompt_injection.py` |
| Backup encryption/restore | `services/backup.py` | `test_backup_restore.py` |
| Localhost binding config | `.streamlit/config.toml`, launcher | `test_streamlit_config.py` |
| No external URLs in code/assets | CI grep + `verify_offline.py` | `test_no_remote_references.py` |

### 4.1 Implementation notes (Phase 7, as built)

Test files differ from the names in the table above; the real ones are `tests/security/test_redaction_and_logging.py`,
`test_network_guard.py`, `test_csv_formula_injection.py`, `test_backup_restore.py`, `test_passcode_permissions_wipe.py`,
`test_isolated_parsing.py` and `test_no_remote_references.py`.

- **Logging.** Everything under the `bank_analyzer` logger goes to a size-limited file in `<data dir>/logs`, through a filter
  that redacts the message and drops the raw arguments. Exceptions are written as class name plus file and line only; their
  messages (which can contain data) never reach the file. Records do not propagate to other handlers. The rule for callers is
  to log events, counts and ids, not descriptions or amounts: redaction recognises amounts, long digit runs, IBANs, e-mail
  addresses and URL queries, but it cannot recognise a free-text description.
- **Network guard.** Replaces `socket.connect`, `connect_ex`, `getaddrinfo` and `gethostbyname` so anything but loopback raises
  `NetworkBlocked` (an `OSError`). It covers Python code in this process only; Ollama, Tesseract, the browser and child
  processes are outside it, and native code can bypass it. It is installed by `app_support.run_page` unless
  `BANKAN_BLOCK_OUTBOUND=0`. It is defence in depth, not a sandbox.
- **Export.** CSV only, UTF-8 with BOM. Text cells whose first non-space character is `= + - @`, tab, CR or LF get a leading
  apostrophe; amounts are written from `Money` and stay numeric. The apostrophe is visible in some tools. The file itself is not
  encrypted.
- **Backup.** `BANKBAK1` header (scrypt parameters, salt, nonce) is authenticated as AES-GCM associated data; the passphrase is
  never stored; a wrong passphrase and a damaged file give the same message. Created with SQLite's online backup, integrity-checked
  before encryption and again after decryption. Refused: passphrases under 12 characters, hostile key-derivation settings,
  newer-schema databases, replacing an existing database without confirmation (the replaced one is kept as `*.pre-restore.bak`).
  Backing up while the same connection has an open transaction is refused: SQLite's backup would otherwise wait forever.
  Limits: encrypted in memory (1 GB cap); a weak passphrase can be guessed offline; a backup copied to a synced folder leaves
  the computer.
- **Parser isolation.** PDF, Excel and OCR parsing runs in a child process: wall-clock timeout (killed), memory cap (Windows job
  object assigned right after start, so a tiny window exists; POSIX `RLIMIT_AS` set by the child), minimal environment (no
  proxies or tokens), stderr discarded, reply parsed as JSON only, error types limited to the application's own. The child has
  the same user, files and network access as the app, so this contains crashes and runaway memory, not a deliberate exploit.
- **Passcode.** Optional. scrypt hash, constant-time comparison, lockout that grows after five misses and survives restarts.
  It keeps casual visitors and pages that can reach the local port out of the UI; it does not encrypt the database, and anyone who
  can edit the data folder can remove it.
- **Permissions.** `audit_private_path` reports broad Windows principals (Everyone, Users, Authenticated Users) by reading
  `icacls` output, or group/other bits on POSIX. It reports only. English group names only.
- **Wipe.** Overwrite with zeros, then delete the database, journal files, automatic copies, logs and passcode; the backups folder
  only if asked. A typed phrase is required. The result lists the limits (SSD wear-levelling, sync folders, earlier backups,
  caches and swap, exports).
- **Offline verification.** `scripts/verify_offline.py` scans the application's own files for non-loopback URLs, `<script src>`,
  CSS imports, CDN hosts and telemetry package names, and checks `.streamlit/config.toml` (loopback address, usage statistics
  off, XSRF protection on) and the settings. The dynamic procedure in section 6 is **not automated and was not performed**.

## 5. Localhost Exposure vs. Browser Risk (explained)

- **Network exposure:** binding to `127.0.0.1` means other machines cannot connect. Binding to `0.0.0.0` would expose the app to the LAN. The app refuses to start with a non-loopback address unless an explicit override is set, and the override prints a warning.
- **Browser risk:** the browser is a separate trust domain. Any web page you visit can attempt requests to `http://127.0.0.1:8501`. Mitigations are Streamlit's XSRF/origin protections, not exposing unauthenticated mutating endpoints, and optionally a start-up access token. Other local OS users on a shared machine can also reach loopback ports; use a dedicated OS account.

## 6. Offline Verification Procedure

1. **Static:** `scripts/verify_offline.py --static` scans source, templates and bundled assets for `http(s)://` references (excluding documentation and loopback), `<script src>`, `@import url(`, CDN hosts, telemetry SDK names.
2. **Config:** asserts Streamlit stats disabled, Ollama URL is loopback, outbound guard setting.
3. **Dynamic (best effort):** run the app and a scripted workflow (import → categorize → query) while recording connections:
   - Windows: `Get-NetTCPConnection -OwningProcess <pid>` polling, or Resource Monitor; optionally Wireshark/`pktmon`.
   - macOS: `lsof -i -n -P` / `nettop`, Little Snitch if available.
   - Linux: `ss -tunp`, `strace -f -e trace=network`, or network namespace with no route (`unshare -n`).
   Pass criterion: only loopback connections (127.0.0.1/::1) from the app and Ollama.
4. **Hard test:** disable network adapter / airplane mode and repeat workflow.
5. **Limits:** polling can miss short connections; DNS lookups may not show as sockets; third-party binaries (Ollama, Tesseract, browser) are outside the app guard; test covers only exercised code paths. A packet capture on a firewalled host is the stronger evidence.

## 7. Firewall Guidance (summary; full commands go in README)

- Windows: Windows Defender Firewall outbound block rules for `python.exe` (venv path) and `ollama.exe`, allowing loopback implicitly.
- macOS: `pf` rules or Little Snitch/LuLu; deny outbound for python and ollama.
- Linux: `nftables`/`ufw` with per-user or cgroup match (`meta skuid`) deny outbound for the service user, or run under a dedicated user.
- Caveat: during model download, temporarily allow Ollama, then re-block.

## 8. Ollama Hardening Checklist

- `OLLAMA_HOST=127.0.0.1:11434` (never `0.0.0.0`).
- `OLLAMA_NO_CLOUD=1` if the installed version supports it; confirm by checking release notes/docs and behavior (`ollama --help`, docs for installed version).
- Pre-pull models; verify with `ollama list`; record model digest in settings for change detection.
- Disable automatic updates in the Ollama desktop app where available; document manual update policy.
- Do not use cloud-hosted model tags.

## 9. Residual Risks (to be published in SECURITY.md)

1. Malware or another administrator on the machine can read everything the user can.
2. Without full-disk encryption, a lost device exposes the DB.
3. SQLite file is **not** encrypted by the application by default.
4. App-level network guard is defence in depth, not a sandbox; only OS firewall gives enforceable control.
5. Third-party components (Ollama, Tesseract, browser, Python packages) may behave unexpectedly; pinned versions reduce but do not remove risk.
6. LLM categorizations can be wrong or manipulated; hence human review.
7. Deleted data may persist on SSDs, in swap, OS caches, OneDrive/backups.
8. Parser vulnerabilities could be triggered by hostile files.
9. Browser-to-localhost attacks cannot be completely eliminated.
10. Offline tests cannot prove the absence of network access in all code paths.

## 10. Privacy Summary (basis for PRIVACY.md)

| Question | Answer |
|----------|--------|
| What is collected? | Only what the user imports; nothing is collected about the user |
| Where is it stored? | Local data directory (SQLite, logs without financial content, optional encrypted backups) |
| Is anything transmitted? | No, by design; installation-time downloads (packages, model weights, OCR data) are separate and contain no user data |
| Telemetry? | None |
| Third parties? | None at runtime |
| Deletion? | Batch rollback, purge, full wipe procedure, with disclosed limits |
| Export? | CSV with formula-neutralization; encrypted backup |
