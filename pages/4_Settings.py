"""Settings: accounts, import history (with undo), categories, rules, local AI and data location."""

from __future__ import annotations

import streamlit as st
from app_support import run_page

from bank_analyzer.ai.selftest import run_selftest
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
from bank_analyzer.services.categorization import Categorizer
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


def main(ctx: AppContext) -> None:
    _accounts(ctx)
    _imports(ctx)
    _categories(ctx)
    _rules(ctx)
    _ai(ctx)
    _data(ctx)


run_page("Settings", main)
