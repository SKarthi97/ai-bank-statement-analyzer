"""Built-in merchant and keyword rules (deterministic, no AI).

Each rule maps phrases to a default category name. ``weak`` marks rules where a merchant name is
weak evidence of purpose (a hypermarket sells groceries *and* clothes; a petrol station sells
fuel *and* snacks): those are offered as suggestions that need the user's approval instead of
being treated as settled. A rule with a direction only applies to money in (or out).

Rules are resolved by category *name*; if a user renamed or disabled that category the rule is
skipped rather than guessing a replacement. Order matters: the first matching rule wins, so more
specific phrases come before general ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bank_analyzer.domain.models import AmountSign, CanonicalTransaction
from bank_analyzer.services.normalization import normalize_description


@dataclass(frozen=True, slots=True)
class BuiltinRule:
    phrases: tuple[str, ...]
    category: str
    direction: AmountSign | None = None
    weak: bool = False


_IN, _OUT = AmountSign.IN, AmountSign.OUT

BUILTIN_RULES: tuple[BuiltinRule, ...] = (
    BuiltinRule(("ATM WITHDRAWAL", "ATM CASH", "CASH WITHDRAWAL"), "Cash withdrawals", _OUT),
    BuiltinRule(("REFUND", "REVERSAL", "CHARGEBACK"), "Refunds", _IN),
    BuiltinRule(("PAYROLL", "SALARY", "WAGES", "GEHALT"), "Salary", _IN),
    BuiltinRule(("UBER EATS", "DOORDASH", "DELIVEROO", "GRUBHUB"), "Dining", _OUT),
    BuiltinRule(
        ("NETFLIX", "SPOTIFY", "DISNEY PLUS", "HULU", "YOUTUBE PREMIUM", "ICLOUD"),
        "Subscriptions",
        _OUT,
    ),
    BuiltinRule(("UBER", "LYFT", "TAXI", "METRO", "TRANSIT", "RAILWAY"), "Transport", _OUT),
    BuiltinRule(
        ("SHELL", "EXXON", "CHEVRON", "TEXACO", "PETROL", "GAS STATION", "FUEL"), "Fuel", _OUT, True
    ),
    BuiltinRule(
        ("GROCERY", "SUPERMARKET", "SUPERMARKT", "WHOLE FOODS", "ALDI", "KROGER", "TESCO"),
        "Groceries",
        _OUT,
    ),
    BuiltinRule(("WALMART", "COSTCO", "TARGET", "AMAZON"), "Shopping", _OUT, True),
    BuiltinRule(("RESTAURANT", "CAFE", "COFFEE", "STARBUCKS", "MCDONALD", "PIZZA"), "Dining", _OUT),
    BuiltinRule(("MORTGAGE", "LANDLORD"), "Housing", _OUT),
    BuiltinRule(("RENT", "MIETE"), "Housing", _OUT, True),  # "RENT A CAR" is not housing
    BuiltinRule(
        (
            "POWER AND LIGHT",
            "ELECTRIC",
            "WATER BILL",
            "INTERNET",
            "BROADBAND",
            "UTILITIES",
            "UTILITY",
        ),
        "Utilities",
        _OUT,
    ),
    BuiltinRule(("INSURANCE",), "Insurance", _OUT),
    BuiltinRule(("HOSPITAL", "CLINIC", "DENTAL", "DOCTOR"), "Healthcare", _OUT),
    BuiltinRule(("PHARMACY",), "Healthcare", _OUT, True),
    BuiltinRule(("TUITION", "UNIVERSITY", "COLLEGE"), "Education", _OUT),
    BuiltinRule(("SCHOOL",), "Education", _OUT, True),
    BuiltinRule(
        ("BANK FEE", "SERVICE FEE", "MONTHLY FEE", "ANNUAL FEE", "OVERDRAFT", "INTEREST CHARGE"),
        "Taxes and fees",
        _OUT,
    ),
    BuiltinRule(("LOAN PAYMENT", "LOAN REPAYMENT"), "Loan payments", _OUT),
    BuiltinRule(("TRANSFER",), "Transfers"),
    BuiltinRule(
        ("BROKERAGE", "VANGUARD", "FIDELITY", "INVESTMENT"), "Savings and investments", None, True
    ),
    BuiltinRule(("CINEMA", "MOVIE", "THEATRE", "CONCERT"), "Entertainment", _OUT),
)


@dataclass(frozen=True, slots=True)
class CompiledBuiltin:
    rule: BuiltinRule
    pattern: re.Pattern[str]

    def matches(self, txn: CanonicalTransaction) -> bool:
        if self.rule.direction == _IN and txn.amount_minor <= 0:
            return False
        if self.rule.direction == _OUT and txn.amount_minor >= 0:
            return False
        return self.pattern.search(txn.normalized_description) is not None


def compile_builtins(rules: tuple[BuiltinRule, ...] = BUILTIN_RULES) -> list[CompiledBuiltin]:
    """Compile phrase lists into whole-word patterns on the normalized description."""
    compiled = []
    for rule in rules:
        normalized = [normalize_description(p) for p in rule.phrases]
        if not all(normalized):  # an empty alternative would match almost anywhere
            raise ValueError(f"built-in rule for {rule.category!r} has an empty phrase")
        phrases = "|".join(re.escape(p) for p in normalized)
        compiled.append(
            CompiledBuiltin(rule, re.compile(rf"(?<![A-Z0-9])(?:{phrases})(?![A-Z0-9])"))
        )
    return compiled
