from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from bank_analyzer.database.repositories import ImportRepository
from bank_analyzer.domain.errors import InvalidStateError, NotFoundError
from bank_analyzer.domain.models import Account, AlertStatus
from bank_analyzer.services.anomaly_detection import RULE_NAMES, AnomalyService
from fakes import load_transactions

AS_OF = date(2024, 6, 30)


# Days before the last day for nine ordinary grocery trips. The gaps (8, 5, 9, 5, 9, 5, 10, 6) are
# irregular on purpose: evenly spaced, identical payments would also be a "recurring payment".
TRIP_OFFSETS = [63, 55, 50, 41, 36, 27, 22, 12, 6]


def seed_big_grocery(conn: sqlite3.Connection, account: Account) -> list:  # noqa: ANN401
    """Nine ordinary 50.00 grocery trips, then a 400.00 one on the last day."""
    specs = [
        {
            "desc": "GROCERY MART",
            "amount": -5000,
            "category": "Groceries",
            "day": (AS_OF - timedelta(days=offset)).isoformat(),
        }
        for offset in TRIP_OFFSETS
    ]
    specs.append(
        {
            "desc": "GROCERY MART",
            "amount": -40000,
            "category": "Groceries",
            "day": AS_OF.isoformat(),
        }
    )
    return load_transactions(conn, account.id, specs)


def table_snapshot(conn: sqlite3.Connection, name: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY 1")]  # noqa: S608 - fixed names


class TestRun:
    def test_creates_an_explainable_alert_and_persists_it(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        rows = seed_big_grocery(conn, usd_account)
        service = AnomalyService(conn)
        report = service.run()
        assert report.as_of == AS_OF and report.created == {"A1": 1} and report.total_created == 1
        (alert,) = service.open_alerts()
        assert (
            alert.rule_id == "A1"
            and alert.transaction_id == rows[-1].id
            and alert.status == AlertStatus.OPEN
        )
        assert alert.details["amount_minor"] == 40000 and alert.details["median_minor"] == 5000
        assert (
            alert.details["group_label"] == "Groceries" and len(alert.related_transaction_ids) == 5
        )

    def test_rerunning_never_recreates_an_alert(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        seed_big_grocery(conn, usd_account)
        service = AnomalyService(conn)
        service.run()
        again = service.run()
        assert again.created == {} and again.already_known == 1 and len(service.open_alerts()) == 1

    def test_a_dismissed_alert_stays_dismissed_across_runs(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        seed_big_grocery(conn, usd_account)
        service = AnomalyService(conn)
        service.run()
        dismissed = service.dismiss(service.open_alerts()[0].id)
        assert dismissed.status == AlertStatus.DISMISSED and dismissed.resolved_at
        assert service.run().created == {} and service.open_alerts() == []

    def test_confirm_records_the_decision_and_nothing_else(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        seed_big_grocery(conn, usd_account)
        service = AnomalyService(conn)
        service.run()
        before = {
            t: table_snapshot(conn, t)
            for t in ("transactions", "accounts", "categories", "import_batches")
        }
        confirmed = service.confirm(service.open_alerts()[0].id)
        after = {t: table_snapshot(conn, t) for t in before}
        assert confirmed.status == AlertStatus.CONFIRMED
        assert (
            before == after
        )  # no transaction, account or category was touched: no automatic action

    def test_running_detection_never_modifies_transactions(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        seed_big_grocery(conn, usd_account)
        before = table_snapshot(conn, "transactions")
        AnomalyService(conn).run()
        assert table_snapshot(conn, "transactions") == before

    def test_resolution_rules(self, conn: sqlite3.Connection, usd_account: Account) -> None:
        from bank_analyzer.database.repositories import AlertRepository

        seed_big_grocery(conn, usd_account)
        service = AnomalyService(conn)
        service.run()
        alert_id = service.open_alerts()[0].id
        service.dismiss(alert_id)
        with pytest.raises(InvalidStateError, match="already resolved"):
            service.confirm(alert_id)
        with pytest.raises(NotFoundError):
            service.dismiss("no-such-alert")
        with pytest.raises(ValueError):
            AlertRepository(conn).resolve(alert_id, AlertStatus.OPEN)

    def test_alert_history_is_kept(self, conn: sqlite3.Connection, usd_account: Account) -> None:
        from bank_analyzer.database.repositories import AlertRepository

        seed_big_grocery(conn, usd_account)
        service = AnomalyService(conn)
        service.run()
        service.dismiss(service.open_alerts()[0].id)
        assert len(AlertRepository(conn).list_alerts()) == 1
        assert (
            len(AlertRepository(conn).list_alerts(status=AlertStatus.DISMISSED, rule_id="A1")) == 1
        )

    def test_empty_database_explains_every_silent_rule(self, conn: sqlite3.Connection) -> None:
        report = AnomalyService(conn).run()
        assert report.total_created == 0 and set(report.skipped) == set(RULE_NAMES)
        assert all(reason == "no transactions" for reason in report.skipped.values())

    def test_silent_rules_say_why_when_history_is_short(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        seed_big_grocery(conn, usd_account)  # 63 days of history
        report = AnomalyService(conn).run()
        assert "at least 90 days" in report.skipped["A5"]

    def test_transactions_after_the_as_of_date_do_not_raise_alerts(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        seed_big_grocery(conn, usd_account)
        report = AnomalyService(conn).run(as_of=AS_OF - timedelta(days=1))
        assert report.created == {}

    def test_rolled_back_imports_are_ignored(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        rows = seed_big_grocery(conn, usd_account)
        ImportRepository(conn).rollback(rows[0].import_batch_id)
        assert AnomalyService(conn).run().total_created == 0

    def test_audit_events_carry_ids_not_content(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        seed_big_grocery(conn, usd_account)
        service = AnomalyService(conn)
        service.run()
        service.dismiss(service.open_alerts()[0].id)
        events = " ".join(
            str(tuple(r)) for r in conn.execute("SELECT event, entity_id, count FROM audit_events")
        )
        assert "alert_dismissed" in events and "GROCERY" not in events

    def test_identical_legitimate_rows_from_one_file_are_flagged_but_never_removed(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        """Two real coffees on one day look like a duplicate charge: flag, keep both, let the user decide."""
        load_transactions(
            conn,
            usd_account.id,
            [
                {"desc": "COFFEE BAR", "amount": -450, "day": "2024-06-29"},
                {"desc": "COFFEE BAR", "amount": -450, "day": "2024-06-29"},
            ],
        )
        service = AnomalyService(conn)
        report = service.run()
        assert report.created == {"A3": 1}
        assert service.open_alerts()[0].details["same_import"] is True
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
