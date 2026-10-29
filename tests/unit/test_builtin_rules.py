from __future__ import annotations

import pytest
from test_rules import txn

from bank_analyzer.domain.models import DEFAULT_CATEGORIES
from bank_analyzer.services.builtin_rules import BUILTIN_RULES, compile_builtins

COMPILED = compile_builtins()


def classify(desc: str, amount: int = -1000) -> tuple[str, bool] | None:
    t = txn(desc, amount)
    for builtin in COMPILED:
        if builtin.matches(t):
            return builtin.rule.category, builtin.rule.weak
    return None


def test_every_rule_points_at_a_default_category() -> None:
    names = {name for name, _ in DEFAULT_CATEGORIES}
    assert {r.category for r in BUILTIN_RULES} <= names


@pytest.mark.parametrize(
    ("desc", "amount", "category"),
    [
        ("SALARY ACME CORP", 300000, "Salary"),
        ("GROCERY MART #1234 CARD 5678", -8540, "Groceries"),
        ("CITY POWER AND LIGHT", -12000, "Utilities"),
        ("NETFLIX.COM", -1599, "Subscriptions"),
        ("ATM WITHDRAWAL MAIN ST", -20000, "Cash withdrawals"),
        ("REFUND GROCERY MART", 1250, "Refunds"),
        ("UBER EATS ORDER", -1800, "Dining"),
        ("UBER TRIP HELP", -2200, "Transport"),
        ("TRANSFER TO SAVINGS", -50000, "Transfers"),
        ("TRANSFER FROM CHECKING", 50000, "Transfers"),
        ("HOME INSURANCE PREMIUM", -9000, "Insurance"),
        ("MONTHLY FEE ACCOUNT", -500, "Taxes and fees"),
        ("LOAN PAYMENT AUTO", -30000, "Loan payments"),
    ],
)
def test_expected_categories(desc: str, amount: int, category: str) -> None:
    assert classify(desc, amount) is not None
    assert classify(desc, amount)[0] == category  # type: ignore[index]


@pytest.mark.parametrize(
    "desc",
    ["RENTAL CAR HERTZ", "TARGETED ADS LTD", "SHELLFISH MARKET", "COSTCOS", "XUBER"],
)
def test_matching_is_whole_word_not_substring(desc: str) -> None:
    result = classify(desc)
    assert result is None or result[0] not in {"Housing", "Shopping", "Fuel", "Transport"}


def test_direction_matters() -> None:
    assert classify("SALARY ACME", -100) is None  # a payment *to* a salary account is not salary
    assert classify("REFUND SHOP", -100) is None
    assert classify("NETFLIX", 500) is None  # money in from Netflix is not a subscription charge
    assert classify("TRANSFER TO X", 500)[0] == "Transfers"  # type: ignore[index]  # any direction


def test_refund_beats_merchant_so_it_is_not_counted_as_groceries() -> None:
    assert classify("REFUND GROCERY MART", 1250)[0] == "Refunds"  # type: ignore[index]


@pytest.mark.parametrize(
    ("desc", "weak"),
    [
        ("WALMART SUPERCENTER", True),  # sells everything: weak evidence of purpose
        ("SHELL 1234", True),  # fuel station also sells food
        ("AMAZON MKTPLACE", True),
        ("RENT MARCH", True),
        ("NETFLIX", False),
        ("SALARY ACME", False),
    ],
)
def test_weak_flag(desc: str, weak: bool) -> None:
    amount = 300000 if "SALARY" in desc else -1000
    result = classify(desc, amount)
    assert result is not None and result[1] is weak


def test_unknown_descriptions_do_not_match() -> None:
    assert classify("XQZ 12 UNKNOWN LTD") is None
    assert classify("ACME BOOKSHOP") is None
