"""Real CSV -> import -> rule categorization -> analytics, reconciliation and alerts, on one statement.

Hand computation from tests/fixtures/deposit_basic.csv (opening balance 500.00):
  January  income 3000.00; expenses 85.40 + 120.00 + 15.99 = 221.39; net 2778.61
  February income 3000.00; expenses 92.10 + 92.10 - 12.50 (refund, unlinked: it is a part-refund of
           a 92.10 purchase) = 171.70; net 2828.30; cash withdrawn 200.00
  raw net per month: Jan 2778.61, Feb 2628.30 (= 2828.30 - 200.00 cash); both match the Phase 2 oracle
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from bank_analyzer.database.repositories import (
    AccountRepository,
    ImportRepository,
    TransactionRepository,
)
from bank_analyzer.domain.imports import ColumnMapping, ImportChoices, SignConvention
from bank_analyzer.domain.models import Account, AccountType, ImportBatch, new_id, utc_now_iso
from bank_analyzer.domain.money import Money
from bank_analyzer.domain.periods import DateRange
from bank_analyzer.services.analytics import UNLINKED_REFUNDS_NAME, Analyzer
from bank_analyzer.services.anomaly_detection import AnomalyService
from bank_analyzer.services.categorization import Categorizer
from bank_analyzer.services.normalization import normalize
from bank_analyzer.services.reconciliation import ReconStatus, reconcile_account
from helpers import raw_from_csv, raw_from_rows

JAN, FEB = DateRange.month("2024-01"), DateRange.month("2024-02")
MAPPING = ColumnMapping(date_col=0, description_col=1, amount_col=2, balance_col=3)


def usd(text: str) -> Money:
    return Money(int(Decimal(text) * 100), "USD")


def import_rows(conn: sqlite3.Connection, account: Account, raw) -> list[str]:  # noqa: ANN001
    choices = ImportChoices(
        account_id=account.id,
        default_currency="USD",
        date_format="%Y-%m-%d",
        decimal_sep=".",
        thousands_sep=",",
        sign_convention=SignConvention.NEGATIVE_IS_OUTFLOW,
    )
    batch_id = new_id()
    result = normalize(raw, MAPPING, choices, import_batch_id=batch_id)
    batch = ImportBatch(
        id=batch_id,
        account_id=account.id,
        file_sha256=raw.file_sha256,
        file_kind=raw.file_kind,
        row_count_read=result.rows_read,
        row_count_imported=len(result.transactions),
        row_count_skipped=0,
        created_at=utc_now_iso(),
    )
    ImportRepository(conn).commit_import(batch, result.transactions)
    return [t.id for t in result.transactions]


def loaded(conn: sqlite3.Connection, usd_account: Account) -> Analyzer:
    import_rows(conn, usd_account, raw_from_csv("deposit_basic.csv"))
    Categorizer(conn).run()
    return Analyzer.from_db(conn)


class TestMonthlyFiguresFromTheRealStatement:
    def test_january_and_february(self, conn: sqlite3.Connection, usd_account: Account) -> None:
        a = loaded(conn, usd_account)
        jan, feb = a.summary(JAN, "USD"), a.summary(FEB, "USD")
        assert (jan.income, jan.expenses, jan.net) == (usd("3000"), usd("221.39"), usd("2778.61"))
        assert (feb.income, feb.expenses, feb.net) == (usd("3000"), usd("171.70"), usd("2828.30"))
        assert feb.cash_withdrawn == usd("200.00") and feb.refunds_netted == usd("12.50")
        assert (jan.raw_net, feb.raw_net) == (usd("2778.61"), usd("2628.30"))  # the Phase 2 oracle

    def test_the_part_refund_is_not_guessed_to_belong_to_a_purchase(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        a = loaded(conn, usd_account)
        spend = {c.name: c.amount for c in a.category_spend(FEB, "USD")}
        assert spend[UNLINKED_REFUNDS_NAME] == usd("-12.50") and spend["Groceries"] == usd("184.20")
        assert a.refunds(FEB, "USD").linked == ()

    def test_savings_rates(self, conn: sqlite3.Connection, usd_account: Account) -> None:
        a = loaded(conn, usd_account)
        assert a.savings_rate(JAN, "USD").value == Decimal("0.9262")  # 2778.61 / 3000
        assert a.savings_rate(FEB, "USD").value == Decimal("0.9428")  # 2828.30 / 3000
        assert a.savings_rate(DateRange.month("2024-03"), "USD").value is None

    def test_category_spend_for_january(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        a = loaded(conn, usd_account)
        assert [(c.name, c.amount) for c in a.category_spend(JAN, "USD")] == [
            ("Utilities", usd("120.00")),
            ("Groceries", usd("85.40")),
            ("Subscriptions", usd("15.99")),
        ]

    def test_month_over_month_and_largest(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        a = loaded(conn, usd_account)
        feb = a.month_over_month(DateRange(JAN.start, FEB.end), "USD", "expenses")[1]
        assert feb.delta == usd("-49.69") and feb.pct == Decimal(
            "-0.2244"
        )  # (171.70-221.39)/221.39
        top = a.largest_transactions(DateRange(JAN.start, FEB.end), "USD", n=2, direction="out")
        assert [t.amount_minor for t in top] == [-20000, -12000]

    def test_analytics_never_need_a_model(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        # Categorizer(conn) above ran with llm=None; every figure was still produced
        assert loaded(conn, usd_account).income(JAN, "USD").value == usd("3000")


class TestReconciliationOfTheRealStatement:
    def test_the_statement_reconciles(self, conn: sqlite3.Connection, usd_account: Account) -> None:
        import_rows(conn, usd_account, raw_from_csv("deposit_basic.csv"))
        (batch,) = reconcile_account(conn, usd_account.id, opening_balance_minor=50000)
        r = batch.result
        assert r.status == ReconStatus.OK and r.steps_checked == 9 and r.opening_check == "ok"

    def test_a_row_lost_in_import_is_detected_with_its_amount(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        full = raw_from_csv("deposit_basic.csv")
        kept = tuple(
            r for r in full.rows if r.cells[1] != "CITY POWER AND LIGHT"
        )  # the -120.00 row
        import_rows(conn, usd_account, raw_from_rows([list(r.cells) for r in kept], full.headers))
        (batch,) = reconcile_account(conn, usd_account.id)
        m = batch.result.first_mismatch
        assert batch.result.status == ReconStatus.MISMATCH and m is not None
        assert m.delta_minor == -12000  # a 120.00 outflow is unaccounted for
        assert len(TransactionRepository(conn).query().items) == 8  # nothing was invented

    def test_the_balance_check_does_not_depend_on_categories_or_a_model(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        import_rows(conn, usd_account, raw_from_csv("deposit_basic.csv"))
        before = reconcile_account(conn, usd_account.id)[0].result
        Categorizer(conn).run()
        assert reconcile_account(conn, usd_account.id)[0].result == before


class TestAlertsOnTheRealStatement:
    def test_the_identical_grocery_pair_is_flagged_but_both_rows_are_kept(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        import_rows(conn, usd_account, raw_from_csv("deposit_basic.csv"))
        Categorizer(conn).run()
        service = AnomalyService(conn)
        report = service.run()
        assert report.created == {"A3": 1}
        (alert,) = service.open_alerts()
        assert alert.details["same_import"] is True and alert.details["amount_minor"] == 9210
        assert TransactionRepository(conn).query().total == 9  # flagged, never deleted
        service.dismiss(alert.id)
        assert service.run().created == {}


def test_other_accounts_do_not_leak_into_each_others_numbers(
    conn: sqlite3.Connection, usd_account: Account
) -> None:
    other = AccountRepository(conn).create("Other", AccountType.DEPOSIT, "USD")
    import_rows(conn, usd_account, raw_from_csv("deposit_basic.csv"))
    Categorizer(conn).run()
    assert Analyzer.from_db(conn, account_id=other.id).summary(JAN, "USD").transaction_count == 0
    assert (
        Analyzer.from_db(conn, account_id=usd_account.id).summary(JAN, "USD").transaction_count == 4
    )
