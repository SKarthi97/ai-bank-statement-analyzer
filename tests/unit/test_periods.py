from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bank_analyzer.domain.periods import (
    DateRange,
    last_day_of_month,
    month_key,
    months_in,
    previous_month,
)


class TestDateRange:
    def test_start_after_end_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            DateRange(date(2024, 2, 2), date(2024, 2, 1))

    def test_contains_is_inclusive_and_days_counts_both_ends(self) -> None:
        r = DateRange(date(2024, 1, 1), date(2024, 1, 31))
        assert r.contains(date(2024, 1, 1)) and r.contains(date(2024, 1, 31))
        assert not r.contains(date(2023, 12, 31)) and not r.contains(date(2024, 2, 1))
        assert r.days == 31 and DateRange(date(2024, 1, 5), date(2024, 1, 5)).days == 1

    def test_label(self) -> None:
        assert DateRange.month("2024-02").label() == "2024-02-01 to 2024-02-29"

    @pytest.mark.parametrize(
        ("start", "end", "whole"),
        [
            (date(2024, 1, 1), date(2024, 1, 31), True),
            (date(2024, 2, 1), date(2024, 2, 29), True),  # leap year
            (date(2023, 2, 1), date(2023, 2, 28), True),
            (date(2024, 2, 1), date(2024, 2, 28), False),  # not the last day in a leap year
            (date(2024, 1, 1), date(2024, 12, 31), True),
            (date(2024, 1, 1), date(2024, 3, 31), True),
            (date(2024, 1, 2), date(2024, 1, 31), False),
            (date(2024, 1, 1), date(2024, 1, 30), False),
        ],
    )
    def test_whole_months(self, start: date, end: date, whole: bool) -> None:
        assert DateRange(start, end).is_whole_months is whole

    def test_constructors(self) -> None:
        assert DateRange.month("2023-12") == DateRange(date(2023, 12, 1), date(2023, 12, 31))
        assert DateRange.year(2024) == DateRange(date(2024, 1, 1), date(2024, 12, 31))


class TestLastNMonths:
    @pytest.mark.parametrize(
        ("today", "n", "start", "end"),
        [
            (date(2024, 3, 15), 1, date(2024, 2, 1), date(2024, 2, 29)),
            (date(2024, 3, 15), 3, date(2023, 12, 1), date(2024, 2, 29)),  # crosses the year
            (date(2024, 1, 10), 1, date(2023, 12, 1), date(2023, 12, 31)),
            (date(2024, 1, 1), 2, date(2023, 11, 1), date(2023, 12, 31)),
            (date(2024, 12, 31), 12, date(2023, 12, 1), date(2024, 11, 30)),
            (date(2024, 6, 30), 24, date(2022, 6, 1), date(2024, 5, 31)),
        ],
    )
    def test_complete_months_before_today(
        self, today: date, n: int, start: date, end: date
    ) -> None:
        period = DateRange.last_n_months(today, n)
        assert (period.start, period.end) == (start, end)
        assert period.is_whole_months and len(months_in(period)) == n

    def test_the_current_month_is_never_included(self) -> None:
        today = date(2024, 5, 20)
        assert not DateRange.last_n_months(today, 3).contains(today)

    def test_n_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            DateRange.last_n_months(date(2024, 1, 1), 0)


class TestMonthHelpers:
    def test_months_in(self) -> None:
        assert months_in(DateRange(date(2024, 1, 15), date(2024, 1, 20))) == ["2024-01"]
        assert months_in(DateRange(date(2023, 11, 30), date(2024, 2, 1))) == [
            "2023-11",
            "2023-12",
            "2024-01",
            "2024-02",
        ]

    def test_previous_month(self) -> None:
        assert previous_month("2024-03") == "2024-02"
        assert previous_month("2024-01") == "2023-12"

    def test_month_key_and_last_day(self) -> None:
        assert month_key(date(2024, 2, 9)) == "2024-02"
        assert [
            last_day_of_month(2024, 2),
            last_day_of_month(2023, 2),
            last_day_of_month(2024, 12),
        ] == [
            29,
            28,
            31,
        ]


@given(
    st.dates(min_value=date(2001, 1, 1), max_value=date(2099, 12, 31)),
    st.integers(min_value=1, max_value=36),
)
def test_property_last_n_months_is_n_whole_months_ending_before_today(today: date, n: int) -> None:
    period = DateRange.last_n_months(today, n)
    assert period.is_whole_months and len(months_in(period)) == n
    assert period.end < date(today.year, today.month, 1)
