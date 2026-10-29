"""Builders for analytics tests: in-memory transactions and the default categories with roles."""

from __future__ import annotations

from datetime import date

from bank_analyzer.domain.models import (
    DEFAULT_CATEGORIES,
    DEFAULT_ROLES,
    CanonicalTransaction,
    Category,
    CategoryRole,
    CategorySource,
    ReviewStatus,
    SourceRef,
    new_id,
    utc_now_iso,
)
from bank_analyzer.services.normalization import merchant_key, normalize_description

CATEGORIES: dict[str, Category] = {
    name: Category(
        id=f"cat-{name}",
        name=name,
        kind=kind,
        role=DEFAULT_ROLES.get(name, CategoryRole.EXPENSE),
        is_default=True,
        created_at="2024-01-01T00:00:00Z",
    )
    for name, kind in DEFAULT_CATEGORIES
}
BY_ID: dict[str, Category] = {c.id: c for c in CATEGORIES.values()}


def money(text: str) -> int:
    """'-85.40' -> -8540 (two-decimal currencies only; keeps test data readable)."""
    sign = -1 if text.startswith("-") else 1
    whole, _, frac = text.lstrip("+-").partition(".")
    return sign * (int(whole) * 100 + int((frac + "00")[:2]))


def make(
    day: str,
    desc: str,
    amount: str,
    category: str | None = None,
    *,
    account: str = "A",
    currency: str = "USD",
    group: str | None = None,
    voided: bool = False,
    row: int = 1,
    balance: str | None = None,
    batch: str = "batch-1",
) -> CanonicalTransaction:
    normalized = normalize_description(desc)
    now = utc_now_iso()
    return CanonicalTransaction(
        id=new_id(),
        account_id=account,
        import_batch_id=batch,
        txn_date=date.fromisoformat(day),
        original_description=desc,
        normalized_description=normalized,
        merchant_key=merchant_key(normalized),
        amount_minor=money(amount),
        currency=currency,
        reported_balance_minor=None if balance is None else money(balance),
        category_id=CATEGORIES[category].id if category else None,
        category_source=CategorySource.USER_MANUAL if category else CategorySource.NONE,
        review_status=ReviewStatus.APPROVED if category else ReviewStatus.NEEDS_REVIEW,
        transfer_group_id=group,
        source_ref=SourceRef(file_sha256="d" * 64, row_index=row),
        is_voided=voided,
        created_at=now,
        updated_at=now,
    )


def household() -> dict[str, CanonicalTransaction]:
    """Three USD accounts (A checking, B savings, C credit card) plus one EUR account (D).

    Every expected figure in test_analytics.py is hand-computed from this list.
    """
    rows: dict[str, CanonicalTransaction] = {}

    def add(key: str, *args: object, **kwargs: object) -> None:
        rows[key] = make(*args, **kwargs)  # type: ignore[arg-type]

    # ---- January 2024
    add("jan_salary", "2024-01-02", "SALARY ACME CORP", "3000.00", "Salary")
    add("jan_rent", "2024-01-03", "RENT JANUARY", "-1200.00", "Housing")
    add("jan_grocery", "2024-01-05", "GROCERY MART", "-85.40", "Groceries")
    add("jan_atm", "2024-01-08", "ATM WITHDRAWAL MAIN ST", "-200.00", "Cash withdrawals")
    add("jan_to_savings", "2024-01-10", "TRANSFER TO SAVINGS", "-500.00", "Transfers", group="g1")
    add(
        "jan_from_checking",
        "2024-01-10",
        "TRANSFER FROM CHECKING",
        "500.00",
        "Transfers",
        account="B",
        group="g1",
    )
    add("jan_card_pay", "2024-01-12", "CREDIT CARD PAYMENT", "-300.00", "Transfers", group="g2")
    add(
        "jan_card_recv",
        "2024-01-12",
        "PAYMENT THANK YOU",
        "300.00",
        "Transfers",
        account="C",
        group="g2",
    )
    add("jan_vanguard", "2024-01-18", "VANGUARD", "-150.00", "Savings and investments")
    add("jan_restaurant", "2024-01-15", "CITY RESTAURANT", "-60.00", "Dining", account="C")
    add("jan_shop", "2024-01-20", "SHOP", "-120.00", "Shopping", account="C")
    add("jan_shop_refund", "2024-01-22", "REFUND SHOP", "120.00", "Refunds", account="C")
    add("jan_mystery_in", "2024-01-25", "MYSTERY DEPOSIT", "50.00")
    add("jan_mystery_out", "2024-01-28", "MYSTERY CHARGE", "-25.00")
    add("jan_friend", "2024-01-30", "TRANSFER TO FRIEND", "-40.00", "Transfers")
    # ---- February 2024
    add("feb_salary", "2024-02-01", "SALARY ACME CORP", "3000.00", "Salary")
    add("feb_rent", "2024-02-03", "RENT FEBRUARY", "-1200.00", "Housing")
    add("feb_grocery", "2024-02-07", "GROCERY MART", "-92.10", "Groceries")
    add("feb_restaurant", "2024-02-14", "CITY RESTAURANT", "-80.00", "Dining", account="C")
    add("feb_refund_unlinked", "2024-02-20", "REFUND AIRLINE", "45.00", "Refunds")
    add("feb_atm", "2024-02-25", "ATM WITHDRAWAL MAIN ST", "-100.00", "Cash withdrawals")
    # ---- a second currency, kept strictly separate
    add(
        "eur_salary",
        "2024-01-05",
        "SALARY EURO GMBH",
        "2000.00",
        "Salary",
        account="D",
        currency="EUR",
    )
    add("eur_rent", "2024-01-06", "MIETE", "-800.00", "Housing", account="D", currency="EUR")
    return rows
