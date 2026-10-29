"""Reporting periods. Every metric states the period it covers; periods are inclusive civil dates."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def month_key(day: date) -> str:
    """``YYYY-MM`` for a date."""
    return f"{day.year:04d}-{day.month:02d}"


@dataclass(frozen=True, slots=True)
class DateRange:
    """An inclusive range of dates."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("the period start must not be after its end")

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def is_whole_months(self) -> bool:
        """True if the range starts on the 1st and ends on the last day of a month."""
        return self.start.day == 1 and self.end.day == last_day_of_month(
            self.end.year, self.end.month
        )

    def label(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()}"

    @classmethod
    def month(cls, key: str) -> DateRange:
        """The calendar month ``YYYY-MM``."""
        year, month = (int(part) for part in key.split("-"))
        return cls(date(year, month, 1), date(year, month, last_day_of_month(year, month)))

    @classmethod
    def year(cls, year: int) -> DateRange:
        return cls(date(year, 1, 1), date(year, 12, 31))

    @classmethod
    def last_n_months(cls, today: date, n: int) -> DateRange:
        """The ``n`` complete calendar months before the month containing ``today``."""
        if n < 1:
            raise ValueError("n must be at least 1")
        end = date(today.year, today.month, 1) - timedelta(days=1)
        start_month = end.month - (n - 1)
        start_year = end.year
        while start_month < 1:
            start_month += 12
            start_year -= 1
        return cls(date(start_year, start_month, 1), end)


def months_in(period: DateRange) -> list[str]:
    """Every calendar month touched by ``period``, oldest first."""
    keys: list[str] = []
    year, month = period.start.year, period.start.month
    while (year, month) <= (period.end.year, period.end.month):
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return keys


def previous_month(key: str) -> str:
    year, month = (int(part) for part in key.split("-"))
    month -= 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"
