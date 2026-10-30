"""Import Center: upload, choose how to read the file, preview, and confirm.

Nothing is saved until the final confirmation. Every choice that could be guessed wrongly (date
format, separators, which sign means money out) is made by the user; the page only proposes.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st
from app_support import run_page
from pydantic import ValidationError

from bank_analyzer.ai.statement_reader import Understanding, understand
from bank_analyzer.database.repositories import AccountRepository
from bank_analyzer.domain.dates import DATE_FORMATS, detect_date_format
from bank_analyzer.domain.errors import NeedsOcrError, PasswordRequiredError, WrongPasswordError
from bank_analyzer.domain.imports import ColumnMapping, ImportChoices, RawTable, SignConvention
from bank_analyzer.domain.models import AccountType
from bank_analyzer.importer.column_mapper import infer_sign_convention, suggest_mapping
from bank_analyzer.importer.heuristics import dates_have_no_year
from bank_analyzer.importer.isolated import extract_table_isolated
from bank_analyzer.security.file_security import staged_upload
from bank_analyzer.services.categorization import Categorizer
from bank_analyzer.services.import_service import commit_preview, prepare_preview
from bank_analyzer.ui.context import AppContext
from bank_analyzer.ui.display import safe_cell, transaction_rows

NONE = -1
THOUSANDS = {"comma ( , )": ",", "dot ( . )": ".", "space": " ", "none": None}


def _account_section(ctx: AppContext) -> str | None:
    repo = AccountRepository(ctx.conn)
    accounts = repo.list()
    with (
        st.expander("Create an account", expanded=not accounts),
        st.form("new_account", clear_on_submit=True),
    ):
        alias = st.text_input("Name (an alias such as 'Main checking'; no account numbers)")
        kind = st.selectbox("Type", [t.value for t in AccountType])
        currency = st.text_input("Currency (ISO code, e.g. USD)", value="USD", max_chars=3)
        if st.form_submit_button("Create account"):
            repo.create(alias, AccountType(kind), currency.upper())
            st.rerun()
    if not accounts:
        st.info("Create an account first.")
        return None
    labels = {a.id: f"{a.alias} ({a.account_type.value}, {a.currency})" for a in accounts}
    return st.selectbox("Import into", list(labels), format_func=lambda i: labels[i])


def _read_upload(ctx: AppContext) -> None:
    uploaded = st.file_uploader("Statement file", type=["csv", "xlsx", "pdf"])
    use_ocr = st.checkbox("This PDF is a scan: use local OCR (Tesseract must be installed)")
    password = st.text_input(
        "PDF password (only if the PDF asks for one)",
        type="password",
        help="Used once to open this file. It is held in memory only, never saved, logged or sent "
        "anywhere. The imported transactions are saved in the app's database like any other import.",
    )
    if uploaded is None or not st.button("Read file"):
        return
    st.session_state.pop("preview", None)
    st.session_state.pop("smart", None)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "upload.bin"
        path.write_bytes(uploaded.getvalue())
        s = ctx.settings
        try:
            with staged_upload(
                path,
                original_name=uploaded.name,
                max_mb_tabular=s.max_upload_mb_tabular,
                max_mb_pdf=s.max_upload_mb_pdf,
            ) as staged:
                raw = extract_table_isolated(
                    staged,
                    max_pdf_pages=s.max_pdf_pages,
                    use_ocr=use_ocr,
                    tesseract_cmd=s.tesseract_cmd,
                    password=password or None,
                )
        except NeedsOcrError as exc:
            st.warning(f"{exc} Tick the OCR box and read the file again.")
            return
        except PasswordRequiredError as exc:
            st.warning(str(exc))
            return
        except WrongPasswordError as exc:
            st.error(str(exc))
            return
    st.session_state["raw"] = raw
    st.session_state["file_name"] = uploaded.name


def _smart_section(
    ctx: AppContext, raw: RawTable, account_id: str
) -> tuple[RawTable, ColumnMapping, ImportChoices] | None:
    """Optional: let the local model work out the layout, then test it against the document."""
    st.subheader("Optional: let the local AI read the layout")
    st.caption(
        "Rules try first (no AI needed). Only if they cannot read the layout is the local model "
        "(on this computer) shown the column headings, a few sample rows and the text around the "
        "table. Neither ever supplies amounts, and a reading is offered for use only if the "
        "document's own balances and totals agree with it."
    )
    if st.button("Analyze the layout"):
        account = AccountRepository(ctx.conn).get(account_id)
        with st.spinner("Reading the layout (if the local AI is needed, the first run is slow)..."):
            st.session_state["smart"] = understand(
                ctx.llm(), raw, account_id=account_id, default_currency=account.currency
            )
    smart: Understanding | None = st.session_state.get("smart")
    if smart is None:
        return None
    if smart.application is None or smart.check is None or smart.reading.layout is None:
        st.warning(f"No usable reading was obtained: {smart.problem}. Use the manual steps.")
        return None
    how = "rules (no AI needed)" if smart.source == "rules" else "the local AI"
    st.caption(f"Layout worked out by {how}.")
    for repair in smart.repairs:
        st.info(repair)
    layout, application = smart.reading.layout, smart.application
    st.write(
        f"**Bank:** {safe_cell(layout.bank_name or 'not stated', 80)} | **Currency:** {layout.currency} | "
        f"**Rows read:** {len(application.table.rows)}"
    )
    for note in application.notes:
        st.info(note)
    st.dataframe(
        [
            {"Check": c.name, "Result": "agrees" if c.ok else "DOES NOT AGREE", "Detail": c.detail}
            for c in smart.check.items
        ]
    )
    if application.opening_minor is not None and application.closing_minor is not None:
        st.write(
            f"Opening balance {application.opening_minor / 100:,.2f} -> closing balance "
            f"{application.closing_minor / 100:,.2f} {layout.currency}"
        )
    account = AccountRepository(ctx.conn).get(account_id)
    if layout.currency != account.currency:
        st.warning(
            f"The statement is in {layout.currency} but this account is in {account.currency}. "
            f"Open 'Create an account' above, create one in {layout.currency}, select it under "
            "'Import into', and analyze the layout again."
        )
        return None
    if not smart.trustworthy:
        st.warning(
            "The document's numbers do NOT all agree with the AI's reading, or there was nothing "
            "to check it against. Do not use it blindly: use the manual steps below."
        )
        return None
    st.success("The document's own balances and totals agree with this reading.")
    if st.checkbox("Use this reading (skip the manual column and format steps)"):
        return application.table, application.mapping, application.choices
    return None


def _mapping_section(raw: RawTable) -> ColumnMapping | None:
    st.subheader("2. Which column is which?")
    suggestion = suggest_mapping(raw.headers)
    for note in suggestion.notes:
        st.warning(note)
    st.dataframe(
        [
            {h: safe_cell(c) for h, c in zip(raw.headers, r.cells, strict=False)}
            for r in raw.rows[:8]
        ]
    )
    options = [NONE, *range(len(raw.headers))]

    def pick(label: str, key: str, required: bool = False) -> int | None:
        default = suggestion.found.get(key, NONE)
        choice = st.selectbox(
            label,
            options,
            index=options.index(default),
            format_func=lambda i: "(none)" if i == NONE else safe_cell(raw.headers[i], 60),
            key=f"map_{key}",
        )
        return None if choice == NONE else int(choice)

    has_split = "debit" in suggestion.found or "credit" in suggestion.found
    style = st.radio(
        "Amounts are in",
        ["one amount column", "separate debit and credit columns"],
        index=1 if has_split and "amount" not in suggestion.found else 0,
    )
    date_col, desc_col = pick("Date", "date"), pick("Description", "description")
    kwargs: dict[str, int | None] = {"date_col": date_col, "description_col": desc_col}
    if style == "one amount column":
        kwargs["amount_col"] = pick("Amount", "amount")
        kwargs["indicator_col"] = pick("Debit/credit indicator (DR/CR), if any", "indicator")
    else:
        kwargs["debit_col"] = pick("Debit (money out)", "debit")
        kwargs["credit_col"] = pick("Credit (money in)", "credit")
    kwargs["balance_col"] = pick("Running balance (optional)", "balance")
    kwargs["currency_col"] = pick("Currency column (optional)", "currency")
    if date_col is None or desc_col is None:
        st.info("Choose the date and description columns.")
        return None
    try:
        return ColumnMapping(**{k: v for k, v in kwargs.items() if v is not None})  # type: ignore[arg-type]
    except ValidationError:
        st.info("Choose either one amount column or the debit/credit columns.")
        return None


def _choices_section(
    ctx: AppContext, raw: RawTable, mapping: ColumnMapping, account_id: str
) -> ImportChoices | None:
    st.subheader("3. How should values be read?")
    account = AccountRepository(ctx.conn).get(account_id)
    samples = [r.cells[mapping.date_col] for r in raw.rows[:30] if mapping.date_col < len(r.cells)]
    guess = detect_date_format(samples)
    formats = list(guess.candidates) or list(DATE_FORMATS)
    if not guess.candidates and dates_have_no_year(samples):
        st.warning(
            "These dates have no year, so no date format below can read them. Use 'Analyze the "
            "layout' above: it finds the year in the statement's own text."
        )
    elif not guess.candidates:
        st.warning("No single date format fits every row; pick the closest and check the preview.")
    elif guess.ambiguous:
        st.warning("These dates can be read in more than one way. Choose carefully.")
    if guess.two_digit_year:
        st.warning("Two-digit years are guessed; check the dates in the preview.")
    date_format = st.selectbox("Date format", formats)
    decimal_sep = st.selectbox("Decimal separator", [".", ","])
    thousands_label = st.selectbox("Thousands separator", list(THOUSANDS))
    thousands = THOUSANDS[thousands_label]
    if thousands == decimal_sep:
        st.error("The decimal and thousands separators must differ.")
        return None
    currency = st.text_input("Currency of this file", value=account.currency, max_chars=3).upper()
    conventions = [c.value for c in SignConvention]
    if mapping.amount_col is not None and mapping.balance_col is not None:
        hint = infer_sign_convention(
            raw,
            mapping,
            currency=currency,
            decimal_sep=decimal_sep,
            thousands_sep=thousands,
            account_type=account.account_type,
        )
        st.caption(f"Sign check from the running balance: {hint.note}")
    sign = st.selectbox(
        "Which amounts are money going out?",
        conventions,
        help="negative_is_outflow: debits are negative. positive_is_outflow: purchases are "
        "positive (typical for cards).",
    )
    try:
        return ImportChoices(
            account_id=account_id,
            default_currency=currency,
            date_format=date_format,
            decimal_sep=decimal_sep,
            thousands_sep=thousands,
            sign_convention=SignConvention(sign),
        )
    except ValidationError as exc:
        st.error(str(exc))
        return None


def _preview_section(
    ctx: AppContext, raw: RawTable, mapping: ColumnMapping, choices: ImportChoices
) -> None:
    st.subheader("4. Preview")
    if st.button("Preview import (nothing is saved)"):
        st.session_state["preview"] = prepare_preview(
            ctx.conn,
            raw,
            mapping,
            choices,
            display_name=safe_cell(st.session_state.get("file_name"), 100),
        )
    preview = st.session_state.get("preview")
    if preview is None:
        return
    result = preview.result
    a, b, c, d = st.columns(4)
    a.metric("Rows read", result.rows_read)
    b.metric("Will import", len(result.transactions))
    c.metric("Skipped (blank/footer)", result.rows_skipped)
    d.metric("Rejected", result.rows_rejected)
    if result.issues:
        st.write("Problems found:")
        st.dataframe(
            [
                {
                    "Row": i.row_index,
                    "Severity": str(i.severity),
                    "Code": i.code,
                    "Message": safe_cell(i.message),
                }
                for i in result.issues[:100]
            ]
        )
    st.write("First rows as they will be saved:")
    st.dataframe(transaction_rows(result.transactions[:20], {}))

    st.subheader("5. Confirm")
    accept_rejected = confirm_low = allow_reimport = keep_duplicates = False
    skip_ids: set[str] = set()
    if preview.needs_rejected_ack:
        accept_rejected = st.checkbox(
            f"{result.rows_rejected} rows were rejected and will NOT be imported"
        )
    if preview.needs_low_confidence_ack:
        confirm_low = st.checkbox(
            f"I checked the amounts of the {len(result.low_confidence_rows)} low-confidence OCR rows "
            "against the original"
        )
    if preview.needs_reimport_ack:
        allow_reimport = st.checkbox("This exact file was imported before. Import it again anyway")
    if preview.needs_duplicate_decision:
        st.warning(f"{len(preview.duplicates)} rows look like transactions already stored.")
        decision = st.radio(
            "What should happen to them?",
            ["(decide)", "Skip them", "Keep them (they are genuine repeats)"],
        )
        if decision == "Skip them":
            skip_ids = {d.transaction_id for d in preview.duplicates}
        elif decision.startswith("Keep"):
            keep_duplicates = True
    confirmed = st.checkbox("Save this import")
    if st.button("Import", disabled=not confirmed):
        summary = commit_preview(
            ctx.conn,
            preview,
            confirmed=confirmed,
            skip_transaction_ids=skip_ids,
            keep_remaining_duplicates=keep_duplicates,
            accept_rejected_rows=accept_rejected,
            confirm_low_confidence_amounts=confirm_low,
            allow_reimport=allow_reimport,
        )
        st.session_state.pop("preview", None)
        st.session_state["last_import"] = summary.batch_id
        st.success(f"Imported {summary.imported} transactions ({summary.skipped} skipped).")


def _categorize_section(ctx: AppContext) -> None:
    batch_id = st.session_state.get("last_import")
    if not batch_id:
        return
    st.subheader("6. Categorize")
    use_ai = st.checkbox(
        "Also ask the local AI to suggest categories (suggestions need your review)"
    )
    if st.button("Categorize this import"):
        llm = ctx.llm() if use_ai else None
        report = Categorizer(ctx.conn, llm, batch_size=ctx.settings.llm_batch_size).run(
            batch_id=batch_id, use_llm=use_ai
        )
        st.write(f"Categorized {report.applied} of {report.total_pending}.")
        st.write(
            f"Still uncategorized: {report.remaining_uncategorized} (see Transactions > Review)."
        )
        if report.llm_unavailable:
            st.warning("The local AI was not available; only rules were used.")


def main(ctx: AppContext) -> None:
    st.caption(
        "Supported: CSV, Excel (.xlsx), text PDFs, and scanned PDFs through optional local OCR."
    )
    account_id = _account_section(ctx)
    if account_id is None:
        return
    st.subheader("1. Choose a file")
    _read_upload(ctx)
    raw = st.session_state.get("raw")
    if raw is not None:
        for warning in raw.warnings:
            st.warning(warning)
        smart = _smart_section(ctx, raw, account_id)
        if smart is not None:
            _preview_section(ctx, *smart)
        else:
            mapping = _mapping_section(raw)
            if mapping is not None:
                choices = _choices_section(ctx, raw, mapping, account_id)
                if choices is not None:
                    _preview_section(ctx, raw, mapping, choices)
    _categorize_section(ctx)


run_page("Import Center", main)
