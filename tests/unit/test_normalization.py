from __future__ import annotations

from datetime import date

import pytest

from bank_analyzer.domain.errors import MappingError
from bank_analyzer.domain.imports import ColumnMapping, ImportChoices, SignConvention
from bank_analyzer.domain.validation import (
    E_BAD_AMOUNT,
    E_BAD_CURRENCY,
    E_BAD_DATE,
    E_BAD_INDICATOR,
    E_BOTH_DEBIT_CREDIT,
    E_MISSING_DATE,
    E_NO_AMOUNT,
    I_CONTINUATION_MERGED,
    I_FOOTER_SKIPPED,
    W_SIGN_IN_DEBIT_CREDIT,
    W_TWO_DIGIT_YEAR,
)
from bank_analyzer.services.normalization import (
    detect_formats,
    merchant_key,
    normalize,
    normalize_description,
)
from helpers import raw_from_rows

TODAY = date(2025, 1, 1)


def choices(
    convention: SignConvention = SignConvention.NEGATIVE_IS_OUTFLOW,
    *,
    date_format: str = "%Y-%m-%d",
    decimal_sep: str = ".",
    thousands_sep: str | None = ",",
    currency: str = "USD",
) -> ImportChoices:
    return ImportChoices(
        account_id="acct",
        default_currency=currency,
        date_format=date_format,
        decimal_sep=decimal_sep,
        thousands_sep=thousands_sep,
        sign_convention=convention,
    )


SIGNED = ColumnMapping(date_col=0, description_col=1, amount_col=2, balance_col=3)


def run(rows, mapping=SIGNED, ch=None):
    return normalize(
        raw_from_rows(rows), mapping, ch or choices(), import_batch_id="batch", today=TODAY
    )


class TestSignConventions:
    def test_negative_is_outflow(self) -> None:
        r = run([["2024-01-02", "Shop", "-10.00", ""], ["2024-01-03", "Pay", "20.00", ""]])
        assert [t.amount_minor for t in r.transactions] == [-1000, 2000]

    def test_positive_is_outflow_for_card_statements(self) -> None:
        r = run(
            [["2024-01-02", "Shop", "10.00", ""], ["2024-01-03", "Payment", "-20.00", ""]],
            ch=choices(SignConvention.POSITIVE_IS_OUTFLOW),
        )
        assert [t.amount_minor for t in r.transactions] == [-1000, 2000]

    def test_debit_credit_columns(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, debit_col=2, credit_col=3)
        r = run(
            [["2024-01-02", "Shop", "10.00", ""], ["2024-01-03", "Pay", "", "20.00"]],
            mapping,
            choices(SignConvention.DEBIT_CREDIT_COLUMNS),
        )
        assert [t.amount_minor for t in r.transactions] == [-1000, 2000]

    def test_debit_only_mapping(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, debit_col=2)
        r = run(
            [["2024-01-02", "Shop", "10.00"]], mapping, choices(SignConvention.DEBIT_CREDIT_COLUMNS)
        )
        assert r.transactions[0].amount_minor == -1000

    def test_negative_sign_in_debit_column_warns_and_stays_outflow(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, debit_col=2, credit_col=3)
        r = run(
            [["2024-01-02", "Shop", "-10.00", ""]],
            mapping,
            choices(SignConvention.DEBIT_CREDIT_COLUMNS),
        )
        assert r.transactions[0].amount_minor == -1000
        assert W_SIGN_IN_DEBIT_CREDIT in r.issue_counts()

    def test_both_debit_and_credit_rejected(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, debit_col=2, credit_col=3)
        r = run(
            [["2024-01-02", "Odd", "5.00", "5.00"]],
            mapping,
            choices(SignConvention.DEBIT_CREDIT_COLUMNS),
        )
        assert not r.transactions and r.issue_counts() == {E_BOTH_DEBIT_CREDIT: 1}

    def test_both_empty_rejected(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, debit_col=2, credit_col=3)
        r = run(
            [["2024-01-02", "Odd", "", ""]],
            mapping,
            choices(SignConvention.DEBIT_CREDIT_COLUMNS),
        )
        assert r.issue_counts() == {E_NO_AMOUNT: 1}

    def test_indicator_column(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, amount_col=2, indicator_col=3)
        r = run(
            [
                ["2024-01-02", "A", "10.00", "DR"],
                ["2024-01-03", "B", "10.00", "cr"],
                ["2024-01-04", "C", "-10.00", "Debit"],
            ],
            mapping,
            choices(SignConvention.INDICATOR_DR_CR),
        )
        assert [t.amount_minor for t in r.transactions] == [-1000, 1000, -1000]

    def test_unknown_indicator_rejected(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, amount_col=2, indicator_col=3)
        r = run(
            [["2024-01-02", "A", "10.00", "??"]], mapping, choices(SignConvention.INDICATOR_DR_CR)
        )
        assert r.issue_counts() == {E_BAD_INDICATOR: 1}


class TestRejectionsAndNeverGuess:
    def test_ambiguous_amount_rejected_not_repaired(self) -> None:
        r = run([["2024-01-02", "A", "1,23", ""]])
        assert not r.transactions and r.issue_counts() == {E_BAD_AMOUNT: 1}
        assert r.rows_rejected == 1 and r.has_errors

    def test_excess_precision_rejected(self) -> None:
        r = run([["2024-01-02", "A", "1.234", ""]])
        assert r.issue_counts() == {E_BAD_AMOUNT: 1}

    def test_wrong_date_format_rejected(self) -> None:
        r = run([["25/01/2024", "A", "1.00", ""]])
        assert r.issue_counts() == {E_BAD_DATE: 1}

    def test_missing_date_with_amount_rejected(self) -> None:
        r = run([["", "A", "1.00", ""]])
        assert r.issue_counts() == {E_MISSING_DATE: 1}

    def test_missing_amount_rejected(self) -> None:
        assert run([["2024-01-02", "A", "", ""]]).issue_counts() == {E_NO_AMOUNT: 1}

    def test_good_rows_survive_bad_rows(self) -> None:
        r = run([["2024-01-02", "A", "1.00", ""], ["bad", "B", "2.00", ""]])
        assert len(r.transactions) == 1 and r.rows_rejected == 1

    def test_issues_contain_no_statement_content(self) -> None:
        r = run([["2024-01-02", "SECRET-MERCHANT", "9,99,9", ""]])
        for issue in r.issues:
            assert "SECRET" not in issue.message and "9,99" not in issue.message

    def test_two_digit_year_flagged(self) -> None:
        r = run([["25/01/24", "A", "1.00", ""]], ch=choices(date_format="%d/%m/%y"))
        assert W_TWO_DIGIT_YEAR in r.issue_counts()
        assert r.transactions[0].txn_date == date(2024, 1, 25)


class TestCurrencies:
    def test_default_currency_applied(self) -> None:
        r = run([["2024-01-02", "A", "1.00", ""]], ch=choices(currency="EUR"))
        assert r.transactions[0].currency == "EUR"

    def test_per_row_currency_column(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, amount_col=2, currency_col=3)
        r = run(
            [["2024-01-02", "A", "10.00", "usd"], ["2024-01-02", "B", "1500", "JPY"]],
            mapping,
        )
        assert [(t.currency, t.amount_minor) for t in r.transactions] == [
            ("USD", 1000),
            ("JPY", 1500),
        ]

    def test_invalid_currency_rejected(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, amount_col=2, currency_col=3)
        r = run([["2024-01-02", "A", "10.00", "DOLLARS"]], mapping)
        assert r.issue_counts() == {E_BAD_CURRENCY: 1}


class TestStructureHandling:
    def test_blank_rows_skipped_silently(self) -> None:
        r = run([["", "", "", ""], ["2024-01-02", "A", "1.00", ""]])
        assert r.rows_skipped == 1 and len(r.transactions) == 1 and not r.issues

    def test_footer_rows_skipped_with_info(self) -> None:
        r = run([["2024-01-02", "A", "1.00", ""], ["", "Closing Balance", "", "1.00"]])
        assert r.issue_counts() == {I_FOOTER_SKIPPED: 1} and r.rows_skipped == 1

    def test_totals_row_with_amounts_is_skipped_not_rejected(self) -> None:
        mapping = ColumnMapping(date_col=0, description_col=1, debit_col=2, credit_col=3)
        r = run(
            [["2024-01-02", "Shop", "10.00", ""], ["", "Total", "10.00", "0.00"]],
            mapping,
            choices(SignConvention.DEBIT_CREDIT_COLUMNS),
        )
        assert len(r.transactions) == 1 and r.rows_rejected == 0
        assert r.issue_counts() == {I_FOOTER_SKIPPED: 1}

    def test_continuation_line_merged(self) -> None:
        r = run([["2024-01-02", "ONLINE TRANSFER", "-5.00", ""], ["", "TO SAVINGS", "", ""]])
        assert len(r.transactions) == 1
        assert r.transactions[0].original_description == "ONLINE TRANSFER TO SAVINGS"
        assert I_CONTINUATION_MERGED in r.issue_counts()

    def test_balance_and_booking_date(self) -> None:
        mapping = ColumnMapping(
            date_col=0, description_col=1, amount_col=2, balance_col=3, booking_date_col=4
        )
        r = run([["2024-01-02", "A", "1.00", "10.50", "2024-01-03"]], mapping)
        t = r.transactions[0]
        assert t.reported_balance_minor == 1050 and t.booking_date == date(2024, 1, 3)

    def test_bad_balance_warns_but_imports(self) -> None:
        r = run([["2024-01-02", "A", "1.00", "zz"]])
        assert len(r.transactions) == 1 and r.transactions[0].reported_balance_minor is None
        assert "W_BAD_BALANCE" in r.issue_counts()

    def test_identical_rows_kept_with_occurrence_index(self) -> None:
        rows = [["2024-01-02", "COFFEE", "-3.00", ""]] * 3
        r = run(rows)
        assert [t.occurrence_index for t in r.transactions] == [0, 1, 2]
        assert len({t.id for t in r.transactions}) == 3

    def test_source_reference_preserved(self) -> None:
        r = run([["2024-01-02", "A", "1.00", ""]])
        assert r.transactions[0].source_ref.row_index == 2
        assert r.transactions[0].source_ref.file_sha256 == "0" * 64

    def test_control_characters_removed_from_description(self) -> None:
        r = run([["2024-01-02", "A\x00B​C", "1.00", ""]])
        assert r.transactions[0].original_description == "A B C"

    def test_descriptions_kept_verbatim_apart_from_cleaning(self) -> None:
        r = run([["2024-01-02", '=HYPERLINK("x")', "1.00", ""]])
        assert r.transactions[0].original_description == '=HYPERLINK("x")'


class TestMappingValidation:
    def test_requires_exactly_one_amount_style(self) -> None:
        with pytest.raises(ValueError):
            ColumnMapping(date_col=0, description_col=1)
        with pytest.raises(ValueError):
            ColumnMapping(date_col=0, description_col=1, amount_col=2, debit_col=3)

    def test_unsupported_date_format_rejected(self) -> None:
        with pytest.raises(ValueError):
            choices(date_format="%Y%m%d")

    def test_column_outside_table(self) -> None:
        with pytest.raises(MappingError):
            run([["2024-01-02", "A"]])

    def test_convention_must_match_mapping(self) -> None:
        with pytest.raises(MappingError):
            run([["2024-01-02", "A", "1", "2"]], ch=choices(SignConvention.DEBIT_CREDIT_COLUMNS))
        split = ColumnMapping(date_col=0, description_col=1, debit_col=2)
        with pytest.raises(MappingError):
            run([["2024-01-02", "A", "1"]], split, choices(SignConvention.NEGATIVE_IS_OUTFLOW))
        with pytest.raises(MappingError):
            run([["2024-01-02", "A", "1", "2"]], ch=choices(SignConvention.INDICATOR_DR_CR))


class TestDescriptionNormalization:
    @pytest.mark.parametrize(
        ("original", "normalized", "key"),
        [
            ("GROCERY MART #1234 CARD 5678", "GROCERY MART", "GROCERY MART"),
            ("NETFLIX.COM", "NETFLIX COM", "NETFLIX"),
            ("POS PURCHASE STARBUCKS 01/02 REF: AB12", "POS PURCHASE STARBUCKS", "STARBUCKS"),
            # the bare word CARD stays in the description but is a merchant-key stopword
            ("Card XXXX1234 AMAZON MKTPLACE 123456789", "CARD AMAZON MKTPLACE", "AMAZON MKTPLACE"),
            ("  Shell   Oil  ", "SHELL OIL", "SHELL OIL"),
            ("ATM WITHDRAWAL MAIN ST", "ATM WITHDRAWAL MAIN ST", "ATM WITHDRAWAL MAIN"),
        ],
    )
    def test_cases(self, original: str, normalized: str, key: str) -> None:
        n = normalize_description(original)
        assert n == normalized
        assert merchant_key(n) == key

    def test_empty_gives_no_key(self) -> None:
        assert merchant_key(normalize_description("12345678")) is None

    def test_deterministic(self) -> None:
        assert normalize_description("Foo #1") == normalize_description("Foo #1")


class TestDetectFormats:
    def test_detects_unique(self) -> None:
        raw = raw_from_rows([["2024-01-25", "A", "1,234.50", ""], ["2024-02-01", "B", "-2.00", ""]])
        d = detect_formats(raw, SIGNED, currency="USD")
        assert d.date.candidates == ("%Y-%m-%d",) and not d.needs_user_choice

    def test_reports_ambiguity(self) -> None:
        raw = raw_from_rows([["01/02/2024", "A", "1.00", ""]])
        d = detect_formats(raw, SIGNED, currency="USD")
        assert d.date.ambiguous and d.needs_user_choice


class TestOcrConfidence:
    @staticmethod
    def table(*confidences: float | None):  # noqa: ANN205
        from bank_analyzer.domain.imports import RawRow, RawTable
        from bank_analyzer.domain.models import FileKind

        rows = tuple(
            RawRow(i, (f"2024-01-0{i}", f"SHOP {i}", "-1.00", ""), "page 1", confidence=c)
            for i, c in enumerate(confidences, start=1)
        )
        headers = ("Date", "Description", "Amount", "Balance")
        return RawTable("0" * 64, FileKind.PDF_OCR, headers, rows)

    def test_low_confidence_rows_are_flagged_with_a_warning(self) -> None:
        r = normalize(
            self.table(95.0, 41.0, 79.9, 80.0), SIGNED, choices(), import_batch_id="b", today=TODAY
        )
        assert r.low_confidence_rows == (2, 3)
        assert r.issue_counts() == {"W_LOW_OCR_CONFIDENCE": 2}
        assert len(r.transactions) == 4  # flagged, not dropped: the user decides

    def test_threshold_is_configurable(self) -> None:
        r = normalize(
            self.table(95.0, 85.0),
            SIGNED,
            choices(),
            import_batch_id="b",
            today=TODAY,
            min_ocr_confidence=90.0,
        )
        assert r.low_confidence_rows == (2,)

    def test_rows_without_confidence_are_never_flagged(self) -> None:
        r = normalize(self.table(None, None), SIGNED, choices(), import_batch_id="b", today=TODAY)
        assert r.low_confidence_rows == () and not r.issues

    def test_flagged_row_that_is_also_rejected_is_not_listed(self) -> None:
        from bank_analyzer.domain.imports import RawRow, RawTable
        from bank_analyzer.domain.models import FileKind

        bad = RawRow(1, ("not-a-date", "X", "-1.00", ""), "page 1", confidence=10.0)
        raw = RawTable("0" * 64, FileKind.PDF_OCR, (), (bad,))
        r = normalize(raw, SIGNED, choices(), import_batch_id="b", today=TODAY)
        assert r.low_confidence_rows == () and r.rows_rejected == 1
