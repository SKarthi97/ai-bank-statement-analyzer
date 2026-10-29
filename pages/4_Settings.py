"""Settings: accounts, import history (with undo), categories, rules, local AI, privacy and security."""

from __future__ import annotations

from datetime import datetime

import streamlit as st
from app_support import run_page

from bank_analyzer.ai.selftest import run_selftest
from bank_analyzer.database.maintenance import check_integrity
from bank_analyzer.database.repositories import (
    AccountRepository,
    CategorizationRuleRepository,
    CategoryRepository,
    ImportRepository,
)
from bank_analyzer.domain.models import (
    AmountSign,
    BatchStatus,
    CategoryKind,
    CategoryRole,
    MatchType,
)
from bank_analyzer.security.network_guard import guard_active
from bank_analyzer.security.passcode import (
    PasscodeError,
    clear_passcode,
    has_passcode,
    set_passcode,
)
from bank_analyzer.security.permissions import audit_data_dir
from bank_analyzer.services.backup import (
    SUFFIX,
    backup_notice,
    create_backup,
    verify_backup,
)
from bank_analyzer.services.categorization import Categorizer
from bank_analyzer.services.wipe import CONFIRMATION, wipe_data
from bank_analyzer.ui.context import AppContext
from bank_analyzer.ui.display import safe_cell


def _accounts(ctx: AppContext) -> None:
    st.subheader("Accounts")
    st.dataframe(
        [
            {"Name": safe_cell(a.alias), "Type": a.account_type.value, "Currency": a.currency}
            for a in AccountRepository(ctx.conn).list(active_only=False)
        ],
        hide_index=True,
    )
    st.caption(
        "Create accounts on the Import Center page. Only an alias is stored, never an account number."
    )


def _imports(ctx: AppContext) -> None:
    st.subheader("Import history")
    repo = ImportRepository(ctx.conn)
    accounts = {a.id: a.alias for a in AccountRepository(ctx.conn).list(active_only=False)}
    batches = repo.list_batches()
    st.dataframe(
        [
            {
                "Imported": b.created_at[:19],
                "File": safe_cell(b.display_name),
                "Account": safe_cell(accounts.get(b.account_id, "?")),
                "Rows": b.row_count_imported,
                "Status": b.status.value,
                "ID": b.id,
            }
            for b in batches
        ],
        hide_index=True,
    )
    live = [b for b in batches if b.status == BatchStatus.COMMITTED]
    if not live:
        return
    labels = {
        b.id: f"{b.created_at[:19]}  {safe_cell(b.display_name, 40)}  ({b.row_count_imported} rows)"
        for b in live
    }
    chosen = st.selectbox("Undo an import", list(labels), format_func=lambda i: labels[i])
    st.caption(
        "Undoing hides this import's transactions from every total. The rows are kept so it can be "
        "traced, and other imports are not touched."
    )
    if st.checkbox("I want to undo this import") and st.button("Undo import"):
        summary = repo.rollback(chosen)
        st.success(f"{summary.voided} transactions removed from totals.")
        st.rerun()


def _categories(ctx: AppContext) -> None:
    st.subheader("Categories")
    repo = CategoryRepository(ctx.conn)
    st.dataframe(
        [
            {
                "Name": c.name,
                "Kind": c.kind.value,
                "How it is counted": c.role.value,
                "Active": c.is_active,
            }
            for c in repo.list(active_only=False)
        ],
        hide_index=True,
    )
    with st.form("new_category", clear_on_submit=True):
        name = st.text_input("New category name", max_chars=60)
        kind = st.selectbox("Kind", [k.value for k in CategoryKind])
        role = st.selectbox(
            "How it is counted",
            [r.value for r in CategoryRole],
            index=[r.value for r in CategoryRole].index(CategoryRole.EXPENSE.value),
            help="expense: spending. income: money earned. transfer: between your own accounts. "
            "cash_withdrawal, refund and savings are reported separately.",
        )
        if st.form_submit_button("Add category") and name.strip():
            repo.create(name, CategoryKind(kind), CategoryRole(role))
            st.rerun()


def _rules(ctx: AppContext) -> None:
    st.subheader("Your categorization rules")
    categories = [c for c in CategoryRepository(ctx.conn).list() if c.is_active]
    by_id = {c.id: c.name for c in categories}
    rule_repo = CategorizationRuleRepository(ctx.conn)
    rules = rule_repo.list_rules(active_only=False)
    st.dataframe(
        [
            {
                "Priority": r.priority,
                "Match": r.match_type.value,
                "Pattern": safe_cell(r.pattern),
                "Direction": r.amount_sign.value if r.amount_sign else "any",
                "Category": by_id.get(r.category_id, "?"),
            }
            for r in rules
        ],
        hide_index=True,
    )
    with st.form("new_rule", clear_on_submit=True):
        match = st.selectbox("Match type", [m.value for m in MatchType])
        pattern = st.text_input("Text to match", max_chars=200)
        category = st.selectbox("Category", list(by_id), format_func=lambda i: by_id[i])
        sign = st.selectbox("Only for", ["any", "in", "out"])
        if st.form_submit_button("Add rule"):
            try:
                Categorizer(ctx.conn).add_rule(
                    match_type=MatchType(match),
                    pattern=pattern,
                    category_id=category,
                    amount_sign=None if sign == "any" else AmountSign(sign),
                )
                st.success("Rule added. It applies to future categorization runs.")
            except ValueError as exc:
                st.error(str(exc))


def _ai(ctx: AppContext) -> None:
    st.subheader("Local AI")
    s = ctx.settings
    st.text(f"Server: {s.ollama_url}   Model: {s.model}")
    status = ctx.ai_status()
    (st.success if status.available else st.warning)(status.message)
    st.caption(
        "The model runs on this computer through Ollama. Suggestions are never final until you approve them."
    )
    llm = ctx.llm()
    if llm is not None and st.button("Test the model"):
        result = run_selftest(llm, [c.name for c in CategoryRepository(ctx.conn).list()])
        (st.success if result.usable else st.warning)(
            f"{result.detail} ({result.answered}/{result.total} answered)"
        )


def _data(ctx: AppContext) -> None:
    st.subheader("Where your data is")
    st.text(f"Folder: {ctx.settings.data_dir}")
    st.text(f"Database: {ctx.settings.db_path}")
    st.caption(
        "Statements you upload are read from a private temporary copy that is deleted afterwards. "
        "Nothing is sent to any online service."
    )


def _security(ctx: AppContext) -> None:
    st.subheader("Privacy and security")
    st.text(
        "Outbound network guard: "
        + ("ON (only localhost is reachable from this app)" if guard_active() else "OFF")
    )
    st.caption(
        "The guard covers this app's own code only, not Ollama or your browser. For a firm "
        "guarantee, block outbound access with your operating system's firewall."
    )
    warnings = audit_data_dir(ctx.settings.data_dir, ctx.settings.db_path)
    for warning in warnings:
        st.warning(warning)
    if not warnings:
        st.caption("The data folder looks private to your user account.")
    st.caption(
        "The database file is not encrypted by this app. Use full-disk encryption (BitLocker, FileVault)."
    )


def _health(ctx: AppContext) -> None:
    st.subheader("Database health")
    if st.button("Check database integrity"):
        report = check_integrity(ctx.conn)
        (st.success if report.ok else st.error)(report.summary)
        st.caption(f"Schema version {report.schema_version}.")


def _passcode(ctx: AppContext) -> None:
    st.subheader("Start-up passcode (optional)")
    data_dir = ctx.settings.data_dir
    st.caption(
        "Keeps casual visitors out of the app. It does not encrypt your data: anyone who can read "
        "the files can still read them."
    )
    with st.form("passcode_form", clear_on_submit=True):
        current = st.text_input("Current passcode (if one is set)", type="password")
        new = st.text_input("New passcode (at least 8 characters)", type="password")
        a, b = st.columns(2)
        save = a.form_submit_button("Set or change passcode")
        remove = b.form_submit_button("Remove passcode")
        try:
            if save:
                set_passcode(data_dir, new, current=current)
                st.success("Passcode saved.")
            elif remove:
                clear_passcode(data_dir, current)
                st.success("Passcode removed.")
        except PasscodeError as exc:
            st.error(str(exc))
    st.text("Passcode: " + ("set" if has_passcode(data_dir) else "not set"))


def _backups(ctx: AppContext) -> None:
    st.subheader("Encrypted backup")
    folder = ctx.settings.data_dir / "backups"
    st.caption(backup_notice())
    with st.form("backup_form"):
        passphrase = st.text_input("Passphrase (at least 12 characters)", type="password")
        make = st.form_submit_button("Create backup")
        check = st.form_submit_button("Verify newest backup")
    if make:
        name = f"bankanalyzer-{datetime.now():%Y%m%d-%H%M%S}{SUFFIX}"
        info = create_backup(ctx.conn, folder / name, passphrase)
        st.success(f"Backup written: {name} ({info.size_bytes:,} bytes).")
    if check:
        existing = sorted(folder.glob(f"*{SUFFIX}")) if folder.is_dir() else []
        if not existing:
            st.warning("There are no backups yet.")
        else:
            info = verify_backup(existing[-1], passphrase)
            st.success(f"{existing[-1].name} decrypts and passes its integrity check.")
    st.caption(
        "To restore, close this app and run: python scripts/restore_backup.py <backup file>. "
        "Restoring replaces the database, after keeping a copy of the old one."
    )
    st.text(f"Backups folder: {folder}")


def _wipe(ctx: AppContext) -> None:
    st.subheader("Delete all data")
    st.caption(
        "Overwrites and removes the database, logs and passcode. Backups are kept unless you tick "
        "the box. This is best effort: see the limits shown after deletion."
    )
    with st.form("wipe_form"):
        typed = st.text_input(f"Type {CONFIRMATION} to confirm")
        with_backups = st.checkbox("Also delete the backups folder")
        go = st.form_submit_button("Delete everything")
    if go:
        ctx.close()  # an open database file cannot be removed on Windows
        report = wipe_data(
            ctx.settings.data_dir,
            ctx.settings.db_path,
            confirmation=typed,
            include_backups_folder=with_backups,
        )
        st.success(f"Removed {len(report.removed)} files.")
        if report.failed:
            st.error("Could not remove: " + ", ".join(report.failed))
        for limit in report.limits:
            st.warning(limit)


def main(ctx: AppContext) -> None:
    _accounts(ctx)
    _imports(ctx)
    _categories(ctx)
    _rules(ctx)
    _ai(ctx)
    _security(ctx)
    _health(ctx)
    _passcode(ctx)
    _backups(ctx)
    _data(ctx)
    _wipe(ctx)


run_page("Settings", main)
