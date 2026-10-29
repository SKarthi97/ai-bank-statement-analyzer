from __future__ import annotations

import pytest

from bank_analyzer.domain.imports import ColumnMapping, SignConvention
from bank_analyzer.domain.models import AccountType
from bank_analyzer.importer.column_mapper import (
    find_header_row,
    header_score,
    infer_sign_convention,
    normalize_header,
    suggest_mapping,
)
from helpers import raw_from_rows

MAPPING = ColumnMapping(date_col=0, description_col=1, amount_col=2, balance_col=3)


class TestHeaders:
    def test_normalize(self) -> None:
        assert normalize_header("  Transaction-Date: ") == "transaction date"

    def test_score(self) -> None:
        assert header_score(["Date", "Description", "Amount", "zzz"]) == 3
        assert header_score(["Acme Bank", "Statement"]) == 0

    def test_find_header_row_skips_preamble(self) -> None:
        rows = [["Bank"], ["Account: x"], [], ["Date", "Details", "Debit", "Credit"], ["1"]]
        assert find_header_row(rows) == 3

    def test_single_matching_cell_is_not_enough(self) -> None:
        assert find_header_row([["Date", "foo", "bar"]]) is None


class TestSuggestMapping:
    def test_english_single_amount(self) -> None:
        s = suggest_mapping(["Date", "Description", "Amount", "Balance"])
        assert s.mapping == ColumnMapping(
            date_col=0, description_col=1, amount_col=2, balance_col=3
        )

    def test_german_debit_credit(self) -> None:
        s = suggest_mapping(["Datum", "Beschreibung", "Soll", "Haben", "Saldo"])
        assert s.mapping is not None
        assert (s.mapping.debit_col, s.mapping.credit_col, s.mapping.balance_col) == (2, 3, 4)

    def test_debit_only_is_acceptable(self) -> None:
        s = suggest_mapping(["Date", "Details", "Withdrawals"])
        assert s.mapping is not None and s.mapping.debit_col == 2 and s.mapping.credit_col is None

    def test_extra_columns_ignored(self) -> None:
        s = suggest_mapping(["Ref no", "Date", "foo", "Narrative", "Amount", "Currency"])
        assert s.mapping is not None
        assert (s.mapping.date_col, s.mapping.description_col, s.mapping.currency_col) == (1, 3, 5)

    def test_amount_and_debit_credit_together_is_not_guessed(self) -> None:
        s = suggest_mapping(["Date", "Description", "Amount", "Debit", "Credit"])
        assert s.mapping is None and any("Choose one style" in n for n in s.notes)

    @pytest.mark.parametrize(
        ("headers", "note"),
        [
            (["Description", "Amount"], "No date"),
            (["Date", "Amount"], "No description"),
            (["Date", "Description"], "No amount"),
        ],
    )
    def test_missing_required_columns(self, headers: list[str], note: str) -> None:
        s = suggest_mapping(headers)
        assert s.mapping is None and any(note in n for n in s.notes)

    def test_leftmost_match_wins(self) -> None:
        s = suggest_mapping(["Date", "Posting Date", "Description", "Amount"])
        assert s.mapping is not None and s.mapping.date_col == 0


def rows(amounts_balances: list[tuple[str, str]]) -> list[list[str]]:
    return [[f"2024-01-{i + 1:02d}", "X", a, b] for i, (a, b) in enumerate(amounts_balances)]


def hint(data: list[list[str]], account_type: AccountType = AccountType.DEPOSIT):  # noqa: ANN201
    return infer_sign_convention(
        raw_from_rows(data),
        MAPPING,
        currency="USD",
        decimal_sep=".",
        thousands_sep=",",
        account_type=account_type,
    )


DEPOSIT_SIGNED = [
    ("500.00", "1500.00"),
    ("-100.00", "1400.00"),
    ("-50.00", "1350.00"),
    ("25.00", "1375.00"),
]


class TestSignHint:
    def test_deposit_signed(self) -> None:
        h = hint(rows(DEPOSIT_SIGNED))
        assert h.convention == SignConvention.NEGATIVE_IS_OUTFLOW and h.total_pairs == 3

    def test_deposit_with_positive_debits(self) -> None:
        flipped = [
            ("500.00", "1500.00"),
            ("100.00", "1400.00"),
            ("50.00", "1350.00"),
            ("-25.00", "1375.00"),
        ]
        assert hint(rows(flipped)).convention == SignConvention.POSITIVE_IS_OUTFLOW

    def test_credit_card_balance_is_amount_owed(self) -> None:
        # purchases positive and the owed balance grows with them: positive means outflow
        card = [("0.00", "100.00"), ("40.00", "140.00"), ("10.00", "150.00"), ("-60.00", "90.00")]
        h = hint(rows(card), AccountType.CREDIT_CARD)
        assert h.convention == SignConvention.POSITIVE_IS_OUTFLOW

    def test_newest_first_listing_detected(self) -> None:
        h = hint(rows(DEPOSIT_SIGNED[::-1]))
        assert h.convention == SignConvention.NEGATIVE_IS_OUTFLOW and "newest-first" in h.note

    def test_unreconciled_balance_gives_no_hint(self) -> None:
        broken = [
            ("500.00", "1500.00"),
            ("-100.00", "1400.00"),
            ("-50.00", "9999.00"),
            ("25.00", "1375.00"),
        ]
        h = hint(rows(broken))
        assert h.convention is None and "does not reconcile" in h.note

    def test_too_few_rows(self) -> None:
        assert hint(rows(DEPOSIT_SIGNED[:2])).convention is None

    def test_all_zero_amounts_carry_no_information(self) -> None:
        zeros = [("0.00", "100.00")] * 4
        assert hint(rows(zeros)).convention is None

    def test_needs_balance_and_amount_columns(self) -> None:
        no_balance = ColumnMapping(date_col=0, description_col=1, amount_col=2)
        h = infer_sign_convention(
            raw_from_rows(rows(DEPOSIT_SIGNED)),
            no_balance,
            currency="USD",
            decimal_sep=".",
            thousands_sep=",",
            account_type=AccountType.DEPOSIT,
        )
        assert h.convention is None

    def test_unparsable_rows_are_skipped_not_fatal(self) -> None:
        data = rows(DEPOSIT_SIGNED)
        data.insert(2, ["2024-01-09", "X", "n/a", "n/a"])
        assert hint(data).convention == SignConvention.NEGATIVE_IS_OUTFLOW
