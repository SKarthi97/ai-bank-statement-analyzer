from __future__ import annotations

from datetime import date

import pytest

from bank_analyzer.domain.dates import detect_date_format, has_two_digit_year, parse_date
from bank_analyzer.domain.errors import DateParseError


@pytest.mark.parametrize(
    ("text", "fmt", "expected"),
    [
        ("2024-02-29", "%Y-%m-%d", date(2024, 2, 29)),
        ("25/01/2024", "%d/%m/%Y", date(2024, 1, 25)),
        ("01/25/2024", "%m/%d/%Y", date(2024, 1, 25)),
        ("25.01.2024", "%d.%m.%Y", date(2024, 1, 25)),
        ("05 Mar 2024", "%d %b %Y", date(2024, 3, 5)),
        ("March 5, 2024", "%B %d, %Y", date(2024, 3, 5)),
        (" 2024-01-02 ", "%Y-%m-%d", date(2024, 1, 2)),
    ],
)
def test_parse_valid(text: str, fmt: str, expected: date) -> None:
    assert parse_date(text, fmt) == expected


@pytest.mark.parametrize(
    ("text", "fmt"),
    [
        ("2023-02-29", "%Y-%m-%d"),  # not a leap year
        ("31/04/2024", "%d/%m/%Y"),
        ("13/25/2024", "%m/%d/%Y"),
        ("2024-01-02 extra", "%Y-%m-%d"),
        ("", "%Y-%m-%d"),
        ("01/02/24", "%d/%m/%Y"),
    ],
)
def test_parse_invalid(text: str, fmt: str) -> None:
    with pytest.raises(DateParseError):
        parse_date(text, fmt)


def test_unsupported_format_rejected() -> None:
    with pytest.raises(ValueError):
        parse_date("2024-01-02", "%Y%m%d%H")


def test_leap_day_valid() -> None:
    assert parse_date("2024-02-29", "%Y-%m-%d") == date(2024, 2, 29)


class TestDetection:
    def test_ambiguous_day_month(self) -> None:
        guess = detect_date_format(["01/02/2024", "03/04/2024"])
        assert guess.ambiguous
        assert set(guess.candidates) == {"%d/%m/%Y", "%m/%d/%Y"}

    def test_day_over_12_resolves_dmy(self) -> None:
        guess = detect_date_format(["01/02/2024", "25/01/2024"])
        assert not guess.ambiguous
        assert guess.candidates == ("%d/%m/%Y",)

    def test_month_first_resolved(self) -> None:
        guess = detect_date_format(["01/25/2024", "02/03/2024"])
        assert guess.candidates == ("%m/%d/%Y",)

    def test_iso_unambiguous(self) -> None:
        guess = detect_date_format(["2024-01-02", "2024-12-31"])
        assert guess.candidates == ("%Y-%m-%d",)
        assert not guess.ambiguous

    def test_month_name_forms_collapse(self) -> None:
        # "May" is both an abbreviation and a full name; same date either way.
        guess = detect_date_format(["05 May 2024"])
        assert not guess.ambiguous

    def test_two_digit_year_flagged(self) -> None:
        guess = detect_date_format(["25/01/24"])
        assert guess.two_digit_year
        assert has_two_digit_year("%d/%m/%y")

    def test_no_candidates(self) -> None:
        guess = detect_date_format(["not a date"])
        assert guess.candidates == ()
        assert not guess.ambiguous

    def test_blank_samples_ignored(self) -> None:
        assert detect_date_format(["", "2024-01-02", " "]).candidates == ("%Y-%m-%d",)
