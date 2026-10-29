from __future__ import annotations

import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from analytics_data import make, money
from bank_analyzer.database.repositories import (
    AccountRepository,
    ImportRepository,
)
from bank_analyzer.domain.errors import NotFoundError
from bank_analyzer.domain.models import AccountType
from bank_analyzer.services.reconciliation import (
    ReconStatus,
    reconcile,
    reconcile_account,
    source_order_key,
)
from fakes import load_transactions


def rows(*spec: tuple[str, str | None], **kwargs: object):  # noqa: ANN201
    """(amount, balance) pairs -> transactions in file order with row numbers 1..n."""
    return [
        make("2024-01-05", f"ITEM {i}", amount, balance=balance, row=i, **kwargs)  # type: ignore[arg-type]
        for i, (amount, balance) in enumerate(spec, start=1)
    ]


# A deposit account that starts with 500.00:
#   +1000.00 -> 1500.00 ; -200.00 -> 1300.00 ; -50.00 -> 1250.00
CONSISTENT = [("1000.00", "1500.00"), ("-200.00", "1300.00"), ("-50.00", "1250.00")]


class TestDepositAccounts:
    def test_consistent_statement_with_opening_and_closing_balance(self) -> None:
        result = reconcile(
            rows(*CONSISTENT),
            AccountType.DEPOSIT,
            opening_balance_minor=money("500.00"),
            closing_balance_minor=money("1250.00"),
        )
        assert result.status == ReconStatus.OK and result.mismatch_count == 0
        assert result.steps_checked == 3 and result.orientation == "listed"
        assert (result.opening_check, result.closing_check) == ("ok", "ok")
        assert result.currency == "USD" and result.first_mismatch is None

    def test_without_an_opening_balance_the_first_row_is_the_anchor(self) -> None:
        result = reconcile(rows(*CONSISTENT), AccountType.DEPOSIT)
        assert result.status == ReconStatus.OK and result.steps_checked == 2
        assert result.opening_check is None and result.closing_check is None

    def test_a_wrong_balance_is_reported_with_expected_actual_and_difference(self) -> None:
        data = [*CONSISTENT[:2], ("-50.00", "1200.00")]  # should be 1250.00
        result = reconcile(rows(*data), AccountType.DEPOSIT)
        m = result.first_mismatch
        assert result.status == ReconStatus.MISMATCH and m is not None
        assert (m.row_index, m.expected_minor, m.actual_minor, m.delta_minor) == (
            3,
            money("1250.00"),
            money("1200.00"),
            money("-50.00"),
        )
        assert "50.00 USD" in result.notes[-1] and "none were guessed" in result.notes[-1]

    def test_one_bad_balance_does_not_make_every_later_row_fail(self) -> None:
        data = [
            ("1000.00", "1500.00"),
            ("-200.00", "1250.00"),
            ("-50.00", "1200.00"),
            ("-10.00", "1190.00"),
        ]
        result = reconcile(rows(*data), AccountType.DEPOSIT)
        assert result.mismatch_count == 1 and result.first_mismatch.row_index == 2  # type: ignore[union-attr]

    def test_a_missing_transaction_shows_up_as_its_amount_not_an_invented_row(self) -> None:
        full = rows(*CONSISTENT)
        without_atm = [full[0], full[2]]  # the -200.00 row was not imported
        result = reconcile(without_atm, AccountType.DEPOSIT)
        assert result.first_mismatch is not None
        # the statement shows 200.00 LESS than the rows explain: delta is the missing cash flow
        assert result.first_mismatch.delta_minor == money("-200.00")
        assert len(without_atm) == 2  # nothing was added

    def test_a_duplicated_transaction_shows_up_as_the_opposite_difference(self) -> None:
        full = rows(*CONSISTENT)
        doubled = [full[0], full[1], full[1], full[2]]
        assert reconcile(doubled, AccountType.DEPOSIT).first_mismatch.delta_minor == money("200.00")  # type: ignore[union-attr]

    def test_a_row_without_a_balance_does_not_break_the_chain(self) -> None:
        data = [("1000.00", "1500.00"), ("-200.00", None), ("-50.00", "1250.00")]
        result = reconcile(rows(*data), AccountType.DEPOSIT)
        assert result.status == ReconStatus.OK and result.steps_checked == 1

    def test_missing_balance_hides_but_does_not_invent_a_mismatch_location(self) -> None:
        data = [("1000.00", "1500.00"), ("-200.00", None), ("-50.00", "1000.00")]
        result = reconcile(rows(*data), AccountType.DEPOSIT)
        assert result.first_mismatch.row_index == 3  # reported at the next row that has a balance  # type: ignore[union-attr]

    def test_closing_balance_mismatch(self) -> None:
        result = reconcile(
            rows(*CONSISTENT), AccountType.DEPOSIT, closing_balance_minor=money("1300.00")
        )
        assert result.status == ReconStatus.MISMATCH and result.closing_check == "mismatch"

    def test_opening_balance_mismatch(self) -> None:
        result = reconcile(
            rows(*CONSISTENT), AccountType.DEPOSIT, opening_balance_minor=money("400.00")
        )
        assert result.status == ReconStatus.MISMATCH and result.opening_check == "mismatch"


class TestCardAndLoanAccounts:
    """The balance is the amount owed: purchases (negative cash flow) increase it."""

    CARD = [
        ("-100.00", "100.00"),
        ("-50.00", "150.00"),
        ("120.00", "30.00"),
    ]  # purchase, purchase, payment

    @pytest.mark.parametrize("kind", [AccountType.CREDIT_CARD, AccountType.LOAN])
    def test_owed_balance_reconciles(self, kind: AccountType) -> None:
        result = reconcile(
            rows(*self.CARD), kind, opening_balance_minor=0, closing_balance_minor=money("30.00")
        )
        assert result.status == ReconStatus.OK and result.opening_check == "ok"

    def test_the_same_rows_treated_as_a_deposit_account_do_not_reconcile(self) -> None:
        assert reconcile(rows(*self.CARD), AccountType.DEPOSIT).status == ReconStatus.MISMATCH


class TestNotAvailableIsNotFailure:
    def test_no_balances_at_all(self) -> None:
        result = reconcile(rows(("1.00", None), ("2.00", None)), AccountType.DEPOSIT)
        assert result.status == ReconStatus.NOT_AVAILABLE and result.mismatch_count == 0
        assert "not show enough" in result.notes[0]

    def test_a_single_balance_cannot_be_checked_without_an_opening_balance(self) -> None:
        only_one = rows(("1.00", "10.00"))
        assert reconcile(only_one, AccountType.DEPOSIT).status == ReconStatus.NOT_AVAILABLE
        assert (
            reconcile(only_one, AccountType.DEPOSIT, opening_balance_minor=900).status
            == ReconStatus.OK
        )

    def test_empty_and_mixed_currency(self) -> None:
        assert reconcile([], AccountType.DEPOSIT).status == ReconStatus.NOT_AVAILABLE
        mixed = [
            make("2024-01-05", "A", "1.00", balance="1.00", row=1),
            make("2024-01-05", "B", "1.00", balance="2.00", row=2, currency="EUR"),
        ]
        result = reconcile(mixed, AccountType.DEPOSIT)
        assert (
            result.status == ReconStatus.NOT_AVAILABLE
            and "more than one currency" in result.notes[0]
        )


class TestNewestFirstListings:
    def test_a_reversed_statement_is_detected_and_checked_in_date_order(self) -> None:
        newest_first = rows(*CONSISTENT)[::-1]
        result = reconcile(newest_first, AccountType.DEPOSIT)
        assert result.status == ReconStatus.OK and result.orientation == "newest_first"
        assert "newest first" in result.notes[0]

    def test_opening_balance_applies_to_the_oldest_row_of_a_reversed_statement(self) -> None:
        newest_first = rows(*CONSISTENT)[::-1]
        result = reconcile(
            newest_first,
            AccountType.DEPOSIT,
            opening_balance_minor=money("500.00"),
            closing_balance_minor=money("1250.00"),
        )
        assert result.status == ReconStatus.OK and result.orientation == "newest_first"
        assert (result.opening_check, result.closing_check) == ("ok", "ok")

    def test_genuinely_wrong_data_is_not_rescued_by_flipping(self) -> None:
        bad = rows(("1000.00", "1500.00"), ("-200.00", "9999.00"), ("-50.00", "1250.00"))
        result = reconcile(bad, AccountType.DEPOSIT)
        assert result.status == ReconStatus.MISMATCH and result.orientation == "listed"


@settings(max_examples=120, deadline=None)
@given(
    amounts=st.lists(
        st.integers(min_value=-100_000, max_value=100_000).filter(lambda v: v != 0),
        min_size=2,
        max_size=25,
    ),
    opening=st.integers(min_value=-1_000_000, max_value=1_000_000),
    liability=st.booleans(),
    data=st.data(),
)
def test_property_consistent_statements_reconcile_and_a_tampered_balance_is_located(
    amounts,
    opening,
    liability,
    data,  # noqa: ANN001
) -> None:
    kind = AccountType.CREDIT_CARD if liability else AccountType.DEPOSIT
    balance, built = opening, []
    for i, amount in enumerate(amounts, start=1):
        balance += -amount if liability else amount
        built.append(
            make("2024-01-05", f"ITEM {i}", "1.00", row=i).model_copy(
                update={"amount_minor": amount, "reported_balance_minor": balance}
            )
        )
    ok = reconcile(built, kind, opening_balance_minor=opening, closing_balance_minor=balance)
    assert ok.status == ReconStatus.OK and ok.mismatch_count == 0

    k = data.draw(st.integers(min_value=0, max_value=len(built) - 1))
    delta = data.draw(st.integers(min_value=1, max_value=50_000))
    tampered = list(built)
    tampered[k] = built[k].model_copy(
        update={"reported_balance_minor": built[k].reported_balance_minor + delta}
    )
    bad = reconcile(tampered, kind, opening_balance_minor=opening)
    assert bad.status == ReconStatus.MISMATCH
    assert bad.first_mismatch is not None and bad.first_mismatch.row_index == k + 1
    assert bad.first_mismatch.delta_minor == delta


class TestSourceOrder:
    def test_csv_xlsx_and_pdf_positions(self) -> None:
        csv_row = make("2024-01-05", "A", "1.00", row=7)
        assert source_order_key(csv_row) == (0, 7)
        pdf_row = make("2024-01-05", "A", "1.00", row=3).model_copy(
            update={
                "source_ref": csv_row.source_ref.model_copy(
                    update={"location": "page 2", "row_index": 3}
                )
            }
        )
        assert source_order_key(pdf_row) == (2, 3)

    def test_pdf_rows_sort_by_page_then_row(self) -> None:
        a = make("2024-01-05", "A", "1.00", row=2)
        b = a.model_copy(
            update={
                "source_ref": a.source_ref.model_copy(update={"location": "page 1", "row_index": 9})
            }
        )
        c = a.model_copy(
            update={
                "source_ref": a.source_ref.model_copy(update={"location": "page 2", "row_index": 2})
            }
        )
        assert sorted([c, b], key=source_order_key) == [b, c]
        assert source_order_key(
            a.model_copy(
                update={"source_ref": a.source_ref.model_copy(update={"location": "Sheet1"})}
            )
        ) == (0, 2)


class TestAccountLevel:
    def load(self, conn: sqlite3.Connection, account_id: str, *specs: dict):  # noqa: ANN201
        return load_transactions(conn, account_id, list(specs))

    def test_each_import_is_reconciled_separately_and_opening_applies_to_the_first_only(
        self, conn: sqlite3.Connection
    ) -> None:
        account = AccountRepository(conn).create("Main", AccountType.DEPOSIT, "USD")
        self.load(
            conn,
            account.id,
            {"desc": "A", "amount": 100000, "balance": 150000, "day": "2024-01-02"},
            {"desc": "B", "amount": -20000, "balance": 130000, "day": "2024-01-03"},
        )
        self.load(  # a second statement carries on from 1300.00
            conn,
            account.id,
            {"desc": "C", "amount": -5000, "balance": 125000, "day": "2024-02-02"},
            {"desc": "D", "amount": -1000, "balance": 124000, "day": "2024-02-03"},
        )
        results = reconcile_account(conn, account.id, opening_balance_minor=50000)
        assert [r.result.status for r in results] == [ReconStatus.OK, ReconStatus.OK]
        assert results[0].result.opening_check == "ok" and results[1].result.opening_check is None

    def test_a_bad_statement_is_isolated_and_rolled_back_ones_are_skipped(
        self, conn: sqlite3.Connection
    ) -> None:
        account = AccountRepository(conn).create("Main", AccountType.DEPOSIT, "USD")
        good = self.load(
            conn,
            account.id,
            {"desc": "A", "amount": 100, "balance": 1100},
            {"desc": "B", "amount": 100, "balance": 1200},
        )
        bad = self.load(
            conn,
            account.id,
            {"desc": "C", "amount": 100, "balance": 1000},
            {"desc": "D", "amount": 100, "balance": 5000},
        )
        gone = self.load(conn, account.id, {"desc": "E", "amount": 1, "balance": 1})
        ImportRepository(conn).rollback(gone[0].import_batch_id)
        results = {r.batch_id: r.result.status for r in reconcile_account(conn, account.id)}
        assert results == {
            good[0].import_batch_id: ReconStatus.OK,
            bad[0].import_batch_id: ReconStatus.MISMATCH,
        }

    def test_unknown_account(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(NotFoundError):
            reconcile_account(conn, "nope")
