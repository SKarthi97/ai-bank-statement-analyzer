from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bank_analyzer.domain.errors import AmountParseError, CurrencyMismatch, PrecisionError
from bank_analyzer.domain.money import (
    Money,
    currency_exponent,
    detect_number_format,
    parse_amount,
    sum_money,
    totals_by_currency,
)


def usd(text: str, dec: str = ".", thou: str | None = ",") -> int:
    return parse_amount(text, currency="USD", decimal_sep=dec, thousands_sep=thou).minor


class TestMoneyType:
    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError):
            Money(1.5, "USD")  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        with pytest.raises(TypeError):
            Money(True, "USD")  # type: ignore[arg-type]

    @pytest.mark.parametrize("code", ["usd", "US", "USDX", "", "12$"])
    def test_rejects_bad_currency(self, code: str) -> None:
        with pytest.raises(ValueError):
            Money(1, code)

    def test_arithmetic_is_exact(self) -> None:
        # 0.1 + 0.2 is the classic binary floating point failure.
        assert (Money(10, "USD") + Money(20, "USD")).to_decimal() == Decimal("0.30")

    def test_cross_currency_raises(self) -> None:
        with pytest.raises(CurrencyMismatch):
            Money(1, "USD") + Money(1, "EUR")
        with pytest.raises(CurrencyMismatch):
            assert Money(1, "USD") < Money(2, "EUR")

    def test_negation_abs_sub(self) -> None:
        assert -Money(5, "USD") == Money(-5, "USD")
        assert abs(Money(-5, "USD")) == Money(5, "USD")
        assert Money(5, "USD") - Money(8, "USD") == Money(-3, "USD")

    @pytest.mark.parametrize(
        ("ccy", "exp"), [("USD", 2), ("EUR", 2), ("JPY", 0), ("KWD", 3), ("BHD", 3)]
    )
    def test_exponents(self, ccy: str, exp: int) -> None:
        assert currency_exponent(ccy) == exp

    def test_format(self) -> None:
        assert Money(-123456, "USD").format() == "-1,234.56 USD"
        assert Money(1500, "JPY").format() == "1,500 JPY"
        assert Money(1234, "KWD").format() == "1.234 KWD"

    def test_from_decimal(self) -> None:
        assert Money.from_decimal(Decimal("12.34"), "USD").minor == 1234
        assert Money.from_decimal(Decimal("500"), "JPY").minor == 500
        with pytest.raises(PrecisionError):
            Money.from_decimal(Decimal("12.345"), "USD")
        assert Money.from_decimal(Decimal("12.345"), "USD", round_excess=True).minor == 1234
        assert Money.from_decimal(Decimal("12.355"), "USD", round_excess=True).minor == 1236

    def test_from_decimal_rejects_non_finite(self) -> None:
        with pytest.raises(AmountParseError):
            Money.from_decimal(Decimal("NaN"), "USD")


class TestSumming:
    def test_sum_money_exact_for_many_values(self) -> None:
        items = [Money(1, "USD")] * 10_000
        assert sum_money(items, "USD") == Money(10_000, "USD")

    def test_sum_empty_is_zero(self) -> None:
        assert sum_money([], "EUR") == Money.zero("EUR")

    def test_sum_rejects_mixed(self) -> None:
        with pytest.raises(CurrencyMismatch):
            sum_money([Money(1, "USD"), Money(1, "EUR")], "USD")

    def test_totals_by_currency_never_merges(self) -> None:
        totals = totals_by_currency([Money(100, "USD"), Money(50, "EUR"), Money(-30, "USD")])
        assert totals == {"EUR": Money(50, "EUR"), "USD": Money(70, "USD")}


class TestParseAmountSigns:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12.30", 1230),
            ("+12.30", 1230),
            ("-12.30", -1230),
            ("(12.30)", -1230),
            ("12.30-", -1230),
            ("12.30 DR", -1230),
            ("12.30DR", -1230),
            ("12.30 CR", 1230),
            ("12.30 cr", 1230),
            ("$12.30", 1230),
            ("-$12.30", -1230),
            ("USD 12.30", 1230),
            ("12.30 USD", 1230),
            ("−12.30", -1230),
            ("0.00", 0),
            ("-0.00", 0),
            (".50", 50),
            ("7", 700),
        ],
    )
    def test_sign_and_symbol_forms(self, text: str, expected: int) -> None:
        assert usd(text) == expected

    @pytest.mark.parametrize("text", ["(-12.30)", "-12.30 DR", "-12.30 CR", "(12.30) DR"])
    def test_conflicting_signs_rejected(self, text: str) -> None:
        with pytest.raises(AmountParseError):
            usd(text)


class TestParseAmountSeparators:
    def test_us_format(self) -> None:
        assert usd("1,234.56") == 123456

    def test_european_format(self) -> None:
        assert usd("1.234,56", ",", ".") == 123456

    def test_space_thousands(self) -> None:
        assert usd("1 234,56", ",", " ") == 123456
        assert usd("1 234,56", ",", " ") == 123456  # non-breaking space

    def test_apostrophe_thousands(self) -> None:
        assert usd("1'234.56", ".", "'") == 123456

    def test_indian_grouping(self) -> None:
        assert usd("1,23,456.78") == 12345678

    def test_wrong_grouping_rejected_not_misread(self) -> None:
        for text in ("1,23", "1,2345", "12,34,5"):
            with pytest.raises(AmountParseError):
                usd(text)

    def test_wrong_separator_choice_rejected(self) -> None:
        with pytest.raises(AmountParseError):
            usd("1.234,56")  # decimal '.' but text has comma decimals

    def test_multiple_decimal_separators_rejected(self) -> None:
        with pytest.raises(AmountParseError):
            usd("1.2.3", ".", None)

    def test_thousands_after_decimal_rejected(self) -> None:
        with pytest.raises(AmountParseError):
            usd("1.23,4", ".", ",")

    @pytest.mark.parametrize("text", ["", "  ", "abc", "12a", "-", "()", "."])
    def test_garbage_rejected(self, text: str) -> None:
        with pytest.raises(AmountParseError):
            usd(text)

    def test_invalid_separator_configuration(self) -> None:
        with pytest.raises(ValueError):
            usd("1", ".", ".")
        with pytest.raises(ValueError):
            usd("1", ";", None)


class TestPrecision:
    def test_excess_nonzero_decimals_rejected(self) -> None:
        with pytest.raises(PrecisionError):
            usd("1.234")

    def test_excess_zero_decimals_accepted(self) -> None:
        assert usd("1.2300") == 123

    def test_zero_decimal_currency(self) -> None:
        m = parse_amount("1,500", currency="JPY", decimal_sep=".", thousands_sep=",")
        assert m == Money(1500, "JPY")
        with pytest.raises(PrecisionError):
            parse_amount("1,500.5", currency="JPY", decimal_sep=".", thousands_sep=",")

    def test_three_decimal_currency(self) -> None:
        m = parse_amount("1.234", currency="KWD", decimal_sep=".", thousands_sep=",")
        assert m == Money(1234, "KWD")


class TestFormatDetection:
    def test_unique_us_format(self) -> None:
        guess = detect_number_format(["1,234.56", "10.00"], currency="USD")
        assert not guess.ambiguous
        assert (guess.candidates[0].decimal_sep, guess.candidates[0].thousands_sep) == (".", ",")

    def test_unique_european_format(self) -> None:
        guess = detect_number_format(["1.234,56", "10,00"], currency="USD")
        assert not guess.ambiguous
        assert guess.candidates[0].decimal_sep == ","

    def test_plain_integers_not_ambiguous(self) -> None:
        assert not detect_number_format(["100", "2000"], currency="USD").ambiguous

    def test_three_decimal_currency_is_ambiguous(self) -> None:
        # "1.234" is 1.234 (decimal point) or 1234 (thousands point): the user must choose.
        guess = detect_number_format(["1.234"], currency="KWD")
        assert guess.ambiguous
        assert len(guess.candidates) == 2

    def test_no_fit(self) -> None:
        assert detect_number_format(["abc"], currency="USD").candidates == ()


@given(st.integers(min_value=-(10**15), max_value=10**15))
def test_property_format_parse_roundtrip(minor: int) -> None:
    """Parsing the plain Decimal rendering of any Money returns the same minor units."""
    money = Money(minor, "USD")
    text = format(money.to_decimal(), "f")
    assert parse_amount(text, currency="USD", decimal_sep=".", thousands_sep=None) == money


@given(st.lists(st.integers(min_value=-(10**12), max_value=10**12), max_size=200))
def test_property_sum_equals_integer_sum(values: list[int]) -> None:
    assert sum_money([Money(v, "USD") for v in values], "USD").minor == sum(values)


@given(st.integers(min_value=0, max_value=10**15), st.sampled_from([",", "'", " "]))
def test_property_grouped_rendering_roundtrip(minor: int, sep: str) -> None:
    text = format(Decimal(minor).scaleb(-2), ",.2f").replace(",", sep)
    assert usd(text, ".", sep) == minor
