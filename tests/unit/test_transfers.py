from __future__ import annotations

import sqlite3

import pytest

from analytics_data import BY_ID, make
from bank_analyzer.database.repositories import (
    AccountRepository,
    CategoryRepository,
    TransactionRepository,
)
from bank_analyzer.domain.errors import InvalidStateError, NotFoundError
from bank_analyzer.domain.models import AccountType
from bank_analyzer.domain.periods import DateRange
from bank_analyzer.services.analytics import Analyzer
from bank_analyzer.services.transfers import TransferService, scan_transfers
from fakes import load_transactions

JAN = DateRange.month("2024-01")


def scan(*txns, window: int = 3):  # noqa: ANN002, ANN201
    return scan_transfers(list(txns), BY_ID, window_days=window)


class TestScan:
    def test_a_one_to_one_pair_with_evidence_is_confident(self) -> None:
        out = make("2024-01-10", "TRANSFER TO SAVINGS", "-500.00", "Transfers", account="A")
        inflow = make("2024-01-10", "TRANSFER FROM CHECKING", "500.00", "Transfers", account="B")
        result = scan(out, inflow)
        assert [(p.outflow_id, p.inflow_id) for p in result.confident] == [(out.id, inflow.id)]
        assert result.suggested == () and result.ambiguous == ()
        assert result.confident[0].evidence  # explains why it is trusted

    def test_transfer_wording_alone_is_enough_evidence(self) -> None:
        out = make("2024-01-10", "ONLINE TRANSFER", "-500.00", account="A")  # uncategorized
        inflow = make("2024-01-11", "DEPOSIT", "500.00", account="B")
        assert len(scan(out, inflow).confident) == 1

    def test_credit_card_payment_matches_the_cards_payment_received_leg(self) -> None:
        out = make("2024-01-12", "CREDIT CARD PAYMENT", "-300.00", account="A")
        inflow = make("2024-01-12", "PAYMENT THANK YOU", "300.00", account="C")
        assert len(scan(out, inflow).confident) == 1

    def test_no_evidence_means_suggested_not_confident(self) -> None:
        """Rent out of one account and a salary into another with the same amount and day."""
        rent = make("2024-01-05", "RENT JANUARY", "-900.00", "Housing", account="A")
        pay = make("2024-01-05", "SALARY ACME", "900.00", "Salary", account="B")
        result = scan(rent, pay)
        assert result.confident == () and len(result.suggested) == 1

    @pytest.mark.parametrize(("days", "found"), [(0, True), (3, True), (4, False)])
    def test_date_window(self, days: int, found: bool) -> None:
        out = make("2024-01-10", "TRANSFER", "-50.00", account="A")
        inflow = make(f"2024-01-{10 + days:02d}", "TRANSFER", "50.00", account="B")
        assert bool(scan(out, inflow).confident) is found

    def test_the_inflow_may_land_before_the_outflow_is_booked(self) -> None:
        out = make("2024-01-12", "TRANSFER", "-50.00", account="A")
        inflow = make("2024-01-10", "TRANSFER", "50.00", account="B")
        assert len(scan(out, inflow).confident) == 1

    @pytest.mark.parametrize(
        "inflow_kwargs",
        [
            {"account": "A"},  # same account
            {"account": "B", "currency": "EUR"},  # other currency
            {"account": "B", "voided": True},
            {"account": "B", "group": "g9"},  # already linked
        ],
    )
    def test_non_candidates(self, inflow_kwargs: dict) -> None:
        out = make("2024-01-10", "TRANSFER", "-50.00", account="A")
        inflow = make("2024-01-10", "TRANSFER", "50.00", **inflow_kwargs)
        result = scan(out, inflow)
        assert result.confident == result.suggested == result.ambiguous == ()

    def test_different_amounts_never_pair(self) -> None:
        out = make("2024-01-10", "TRANSFER", "-50.00", account="A")
        assert scan(out, make("2024-01-10", "TRANSFER", "50.01", account="B")).confident == ()

    def test_two_possible_partners_are_ambiguous_and_not_guessed(self) -> None:
        out = make("2024-01-10", "TRANSFER OUT", "-100.00", account="A")
        in1 = make("2024-01-10", "TRANSFER IN", "100.00", account="B")
        in2 = make("2024-01-11", "TRANSFER IN", "100.00", account="C")
        result = scan(out, in1, in2)
        assert result.confident == () and result.suggested == ()
        assert [(a.outflow_ids, sorted(a.inflow_ids)) for a in result.ambiguous] == [
            ((out.id,), sorted([in1.id, in2.id]))
        ]

    def test_two_look_alike_pairs_are_ambiguous(self) -> None:
        o1 = make("2024-01-10", "TRANSFER", "-100.00", account="A")
        o2 = make("2024-01-11", "TRANSFER", "-100.00", account="A")
        i1 = make("2024-01-10", "TRANSFER", "100.00", account="B")
        i2 = make("2024-01-11", "TRANSFER", "100.00", account="B")
        result = scan(o1, o2, i1, i2)
        assert result.confident == () and len(result.ambiguous) == 1
        assert (
            len(result.ambiguous[0].outflow_ids) == 2 and len(result.ambiguous[0].inflow_ids) == 2
        )

    def test_independent_pairs_are_each_found(self) -> None:
        a = [
            make("2024-01-10", "TRANSFER", "-100.00", account="A"),
            make("2024-01-10", "TRANSFER", "100.00", account="B"),
        ]
        b = [
            make("2024-01-20", "TRANSFER", "-70.00", account="A"),
            make("2024-01-20", "TRANSFER", "70.00", account="B"),
        ]
        assert len(scan(*a, *b).confident) == 2


@pytest.fixture
def two_accounts(conn: sqlite3.Connection):  # noqa: ANN201
    accounts = AccountRepository(conn)
    return accounts.create("Checking", AccountType.DEPOSIT, "USD"), accounts.create(
        "Savings", AccountType.DEPOSIT, "USD"
    )


class TestService:
    def seed(self, conn: sqlite3.Connection, a, b):  # noqa: ANN001, ANN201
        out = load_transactions(
            conn,
            a.id,
            [
                {
                    "desc": "TRANSFER TO SAVINGS",
                    "amount": -50000,
                    "category": "Transfers",
                    "day": "2024-01-10",
                }
            ],
        )[0]
        inflow = load_transactions(
            conn,
            b.id,
            [
                {
                    "desc": "TRANSFER FROM CHECKING",
                    "amount": 50000,
                    "category": "Transfers",
                    "day": "2024-01-10",
                }
            ],
        )[0]
        return out, inflow

    def test_linking_moves_the_pair_out_of_unmatched_in_the_reports(
        self, conn: sqlite3.Connection, two_accounts
    ) -> None:
        a, b = two_accounts
        out, inflow = self.seed(conn, a, b)
        before = Analyzer.from_db(conn).summary(JAN, "USD")
        assert (
            before.unmatched_transfers_out.minor == 50000
            and before.unmatched_transfers_in.minor == 50000
        )

        assert TransferService(conn).link_confident() == 1
        after = Analyzer.from_db(conn).summary(JAN, "USD")
        assert after.unmatched_transfers_out.minor == 0 and after.unmatched_transfers_in.minor == 0
        assert after.matched_transfers_net.minor == 0 and after.raw_net == before.raw_net
        txns = TransactionRepository(conn)
        group = txns.get(out.id).transfer_group_id
        assert group is not None and txns.get(inflow.id).transfer_group_id == group
        assert txns.get(out.id).amount_minor == -50000  # amounts are untouched

    def test_only_confident_pairs_are_auto_linked(
        self, conn: sqlite3.Connection, two_accounts
    ) -> None:
        a, b = two_accounts
        rent = load_transactions(
            conn, a.id, [{"desc": "RENT", "amount": -90000, "category": "Housing"}]
        )[0]
        pay = load_transactions(
            conn, b.id, [{"desc": "SALARY", "amount": 90000, "category": "Salary"}]
        )[0]
        service = TransferService(conn)
        assert service.link_confident() == 0
        assert len(service.scan().suggested) == 1
        assert TransactionRepository(conn).get(rent.id).transfer_group_id is None
        assert TransactionRepository(conn).get(pay.id).transfer_group_id is None

    def test_explicit_link_is_the_users_decision_even_without_evidence(
        self, conn: sqlite3.Connection, two_accounts
    ) -> None:
        a, b = two_accounts
        rent = load_transactions(
            conn, a.id, [{"desc": "RENT", "amount": -90000, "day": "2024-01-01"}]
        )[0]
        pay = load_transactions(
            conn, b.id, [{"desc": "WIRE", "amount": 90000, "day": "2024-01-20"}]
        )[0]
        group = TransferService(conn).link(rent.id, pay.id)
        assert TransactionRepository(conn).get(pay.id).transfer_group_id == group

    @pytest.mark.parametrize(
        ("out_amount", "in_amount", "same_account", "message"),
        [
            (-100, 100, True, "same account"),
            (-100, 90, False, "same amount"),
            (100, 100, False, "one outflow and one inflow"),
            (-100, -100, False, "one outflow and one inflow"),
        ],
    )
    def test_explicit_link_validation(
        self,
        conn: sqlite3.Connection,
        two_accounts,
        out_amount: int,
        in_amount: int,
        same_account: bool,
        message: str,
    ) -> None:
        a, b = two_accounts
        first = load_transactions(conn, a.id, [{"desc": "X", "amount": out_amount}])[0]
        second = load_transactions(
            conn, (a if same_account else b).id, [{"desc": "Y", "amount": in_amount}]
        )[0]
        with pytest.raises(InvalidStateError, match=message):
            TransferService(conn).link(first.id, second.id)

    def test_different_currencies_cannot_be_linked(
        self, conn: sqlite3.Connection, two_accounts
    ) -> None:
        a, b = two_accounts
        usd = load_transactions(conn, a.id, [{"desc": "X", "amount": -100}])[0]
        eur = load_transactions(conn, b.id, [{"desc": "Y", "amount": 100, "currency": "EUR"}])[0]
        with pytest.raises(InvalidStateError, match="same amount and currency"):
            TransferService(conn).link(usd.id, eur.id)

    def test_linking_is_atomic_when_a_leg_is_already_linked(
        self, conn: sqlite3.Connection, two_accounts
    ) -> None:
        a, b = two_accounts
        out, inflow = self.seed(conn, a, b)
        other_out = load_transactions(
            conn, a.id, [{"desc": "TRANSFER 2", "amount": -50000, "day": "2024-01-11"}]
        )[0]
        service = TransferService(conn)
        service.link(out.id, inflow.id)
        with pytest.raises(InvalidStateError, match="not yet linked"):
            service.link(other_out.id, inflow.id)  # inflow already taken
        assert TransactionRepository(conn).get(other_out.id).transfer_group_id is None

    def test_unlink_restores_the_unmatched_state(
        self, conn: sqlite3.Connection, two_accounts
    ) -> None:
        a, b = two_accounts
        out, _ = self.seed(conn, a, b)
        service = TransferService(conn)
        service.link_confident()
        group = TransactionRepository(conn).get(out.id).transfer_group_id
        assert group and service.unlink(group) == 2
        assert Analyzer.from_db(conn).summary(JAN, "USD").unmatched_transfers_out.minor == 50000
        with pytest.raises(NotFoundError):
            service.unlink(group)

    def test_link_changes_are_audited_without_content(
        self, conn: sqlite3.Connection, two_accounts
    ) -> None:
        a, b = two_accounts
        self.seed(conn, a, b)
        TransferService(conn).link_confident()
        events = " ".join(
            str(tuple(r)) for r in conn.execute("SELECT event, entity_id, count FROM audit_events")
        )
        assert "transfer_linked" in events and "TRANSFER" not in events

    def test_rolled_back_import_legs_are_not_candidates(
        self, conn: sqlite3.Connection, two_accounts
    ) -> None:
        from bank_analyzer.database.repositories import ImportRepository

        a, b = two_accounts
        out, inflow = self.seed(conn, a, b)
        ImportRepository(conn).rollback(inflow.import_batch_id)
        assert TransferService(conn).scan().confident == ()
        assert CategoryRepository(conn).get_by_name("Transfers")  # sanity: seeds intact
