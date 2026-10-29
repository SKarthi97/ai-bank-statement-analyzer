"""Exact money arithmetic.

Money is stored as an integer count of minor units (cents) plus an ISO 4217 currency code.
Binary floating point is never used: floats are rejected, amounts are parsed from text with
integer arithmetic, and arithmetic across currencies raises instead of guessing.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from bank_analyzer.domain.errors import AmountParseError, CurrencyMismatch, PrecisionError

_CURRENCY_RE = re.compile(r"[A-Z]{3}")

# ISO 4217 minor-unit exponents that differ from the default of 2.
_ZERO_DECIMAL = frozenset(
    {"JPY", "KRW", "CLP", "VND", "ISK", "UGX", "XOF", "XAF", "PYG", "RWF", "VUV", "XPF", "DJF"}
    | {"GNF", "KMF"}
)
_THREE_DECIMAL = frozenset({"BHD", "KWD", "OMR", "JOD", "TND", "IQD", "LYD"})

_SYMBOLS = "$€£¥₹₩₽₺₫฿₪₴₦"
_DR_CR_RE = re.compile(r"(?P<body>.*?)\s*(?P<ind>DR|CR)\.?", re.IGNORECASE | re.DOTALL)

_ALLOWED_DECIMAL = (".", ",")
_ALLOWED_THOUSANDS = (None, ",", ".", " ", "'")


def validate_currency(code: str) -> str:
    """Return ``code`` if it is a well-formed upper-case ISO 4217 code, else raise ValueError."""
    if not isinstance(code, str) or not _CURRENCY_RE.fullmatch(code):
        raise ValueError("currency must be a three-letter upper-case ISO 4217 code")
    return code


def currency_exponent(currency: str) -> int:
    """Number of decimal places for ``currency`` (default 2)."""
    validate_currency(currency)
    if currency in _ZERO_DECIMAL:
        return 0
    if currency in _THREE_DECIMAL:
        return 3
    return 2


@dataclass(frozen=True, slots=True)
class Money:
    """A signed amount in integer minor units with an explicit currency."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise TypeError("Money.minor must be an int (minor units); floats are not allowed")
        validate_currency(self.currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        """The zero amount in ``currency``."""
        return cls(0, currency)

    @classmethod
    def from_decimal(cls, value: Decimal, currency: str, *, round_excess: bool = False) -> Money:
        """Convert a Decimal to Money.

        Excess precision raises PrecisionError unless ``round_excess`` is set, in which case
        banker's rounding is applied. Callers must surface the rounding to the user.
        """
        if not isinstance(value, Decimal) or not value.is_finite():
            raise AmountParseError("amount must be a finite Decimal")
        scaled = value.scaleb(currency_exponent(currency))
        integral = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
        if integral != scaled and not round_excess:
            raise PrecisionError("amount has more decimal places than the currency supports")
        return cls(int(integral), currency)

    def to_decimal(self) -> Decimal:
        """Exact Decimal value."""
        return Decimal(self.minor).scaleb(-currency_exponent(self.currency))

    def format(self) -> str:
        """Human-readable string with thousands separators and the currency code."""
        exp = currency_exponent(self.currency)
        return f"{format(self.to_decimal(), f',.{exp}f')} {self.currency}"

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    def _check(self, other: object) -> Money:
        if not isinstance(other, Money):
            raise TypeError("operation requires another Money")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} with {other.currency}")
        return other

    def __add__(self, other: Money) -> Money:
        return Money(self.minor + self._check(other).minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        return Money(self.minor - self._check(other).minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.minor), self.currency)

    def __lt__(self, other: Money) -> bool:
        return self.minor < self._check(other).minor

    def __le__(self, other: Money) -> bool:
        return self.minor <= self._check(other).minor

    def __gt__(self, other: Money) -> bool:
        return self.minor > self._check(other).minor

    def __ge__(self, other: Money) -> bool:
        return self.minor >= self._check(other).minor


def sum_money(items: Iterable[Money], currency: str) -> Money:
    """Sum amounts that must all be in ``currency``; empty input gives zero."""
    total = 0
    for item in items:
        if item.currency != currency:
            raise CurrencyMismatch(f"cannot combine {currency} with {item.currency}")
        total += item.minor
    return Money(total, currency)


def totals_by_currency(items: Iterable[Money]) -> dict[str, Money]:
    """Per-currency totals. Different currencies are never merged into one figure."""
    totals: dict[str, int] = {}
    for item in items:
        totals[item.currency] = totals.get(item.currency, 0) + item.minor
    return {ccy: Money(minor, ccy) for ccy, minor in sorted(totals.items())}


def _validate_separators(decimal_sep: str, thousands_sep: str | None) -> None:
    if decimal_sep not in _ALLOWED_DECIMAL:
        raise ValueError("decimal separator must be '.' or ','")
    if thousands_sep not in _ALLOWED_THOUSANDS:
        raise ValueError("thousands separator must be one of ',', '.', ' ', \"'\" or None")
    if thousands_sep == decimal_sep:
        raise ValueError("decimal and thousands separators must differ")


def _strip_grouping(int_part: str, thousands_sep: str | None) -> str:
    """Validate thousands grouping and return the bare digits.

    A wrongly grouped number such as ``1,23`` is rejected instead of being read as 123.
    Western (3-digit) and Indian (3 then 2-digit) grouping are accepted.
    """
    if thousands_sep is None or thousands_sep not in int_part:
        if not re.fullmatch(r"[0-9]*", int_part):
            raise AmountParseError("amount contains unexpected characters")
        return int_part
    sep = re.escape(thousands_sep)
    western = rf"[0-9]{{1,3}}(?:{sep}[0-9]{{3}})+"
    indian = rf"[0-9]{{1,2}}(?:{sep}[0-9]{{2}})+{sep}[0-9]{{3}}"
    if not (re.fullmatch(western, int_part) or re.fullmatch(indian, int_part)):
        raise AmountParseError("inconsistent thousands grouping")
    return int_part.replace(thousands_sep, "")


def _unsigned_minor(body: str, exponent: int, decimal_sep: str, thousands_sep: str | None) -> int:
    if not body:
        raise AmountParseError("amount has no digits")
    if thousands_sep != " " and any(ch.isspace() for ch in body):
        raise AmountParseError("amount contains unexpected spaces")
    int_part, _, dec_part = body.partition(decimal_sep)
    if decimal_sep in dec_part:
        raise AmountParseError("amount has more than one decimal separator")
    if thousands_sep and thousands_sep in dec_part:
        raise AmountParseError("thousands separator found after the decimal separator")
    if not re.fullmatch(r"[0-9]*", dec_part):
        raise AmountParseError("amount contains unexpected characters")
    int_digits = _strip_grouping(int_part, thousands_sep)
    if not int_digits and not dec_part:
        raise AmountParseError("amount has no digits")
    if len(dec_part) > exponent:
        if dec_part[exponent:].strip("0"):
            raise PrecisionError("amount has more decimal places than the currency supports")
        dec_part = dec_part[:exponent]
    dec_part = dec_part.ljust(exponent, "0")
    scale = int("1" + "0" * exponent)
    return int(int_digits or "0") * scale + (int(dec_part) if dec_part else 0)


def parse_amount(text: str, *, currency: str, decimal_sep: str, thousands_sep: str | None) -> Money:
    """Parse a statement amount into signed Money using explicit separators.

    Accepted sign forms: leading ``-``/``+``, trailing ``-``, parentheses, and a trailing
    ``DR`` (negative) or ``CR`` (positive). Currency symbols and the currency's own ISO code
    are ignored. Anything ambiguous or malformed raises AmountParseError; excess decimal
    places raise PrecisionError. Separators are never guessed here (see
    ``detect_number_format`` for suggestions that need user confirmation).
    """
    validate_currency(currency)
    _validate_separators(decimal_sep, thousands_sep)
    s = unicodedata.normalize("NFKC", text).replace("’", "'").replace("−", "-").strip()
    if not s:
        raise AmountParseError("amount is empty")

    indicator: str | None = None
    match = _DR_CR_RE.fullmatch(s)
    if match and match.group("body"):
        indicator = match.group("ind").upper()
        s = match.group("body").strip()
    s = re.sub(f"[{re.escape(_SYMBOLS)}]", "", s).strip()
    s = re.sub(rf"(?i)^\s*{currency}\s*|\s*{currency}\s*$", "", s).strip()

    negatives = 0
    if s.startswith("(") and s.endswith(")"):
        negatives += 1
        s = s[1:-1].strip()
    if s[:1] == "-":
        negatives += 1
        s = s[1:].strip()
    elif s[:1] == "+":
        s = s[1:].strip()
    if s[-1:] == "-":
        negatives += 1
        s = s[:-1].strip()
    if indicator == "DR":
        negatives += 1
    if negatives > 1 or (indicator == "CR" and negatives):
        raise AmountParseError("amount has conflicting sign markers")

    minor = _unsigned_minor(s, currency_exponent(currency), decimal_sep, thousands_sep)
    return Money(-minor if negatives else minor, currency)


@dataclass(frozen=True, slots=True)
class NumberFormat:
    """A decimal/thousands separator pair."""

    decimal_sep: str
    thousands_sep: str | None


@dataclass(frozen=True, slots=True)
class NumberFormatGuess:
    """Result of format detection. ``ambiguous`` means the user must choose."""

    candidates: tuple[NumberFormat, ...]
    ambiguous: bool


_FORMAT_ORDER = (
    NumberFormat(".", ","),
    NumberFormat(",", "."),
    NumberFormat(",", " "),
    NumberFormat(".", " "),
    NumberFormat(".", "'"),
    NumberFormat(",", "'"),
    NumberFormat(".", None),
    NumberFormat(",", None),
)


def detect_number_format(samples: Iterable[str], *, currency: str) -> NumberFormatGuess:
    """Find separator pairs under which every sample parses.

    Pairs that give identical results for all samples are collapsed. If more than one distinct
    interpretation remains, ``ambiguous`` is True and nothing may be assumed. If none fit,
    ``candidates`` is empty.
    """
    values = [s for s in samples if s.strip()]
    seen: dict[tuple[int, ...], NumberFormat] = {}
    for fmt in _FORMAT_ORDER:
        try:
            result = tuple(
                parse_amount(
                    v,
                    currency=currency,
                    decimal_sep=fmt.decimal_sep,
                    thousands_sep=fmt.thousands_sep,
                ).minor
                for v in values
            )
        except AmountParseError:
            continue
        seen.setdefault(result, fmt)
    candidates = tuple(seen.values())
    return NumberFormatGuess(candidates=candidates, ambiguous=len(candidates) > 1)
