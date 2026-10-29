"""Balance reconciliation: does the statement's running balance agree with its transactions?

Sign rules (cash-flow amounts: inflow positive, outflow negative):

* deposit, cash and investment accounts: balance = previous balance + amount
* credit-card and loan accounts: the balance is the amount *owed*, so a purchase (negative cash
  flow) increases it: balance = previous balance - amount

Three outcomes, kept distinct on purpose:

* ``not_available`` - the statement carries too few balances to check anything (this is *missing
  data*, not a failure);
* ``ok`` - every checkable step agrees;
* ``mismatch`` - a step disagrees. The first disagreement is reported with the expected balance,
  the actual one and the difference. A missing transaction is *not* guessed or invented; the note
  only says that an amount of that size is unaccounted for.

Rows are checked in the statement's own order, and newest-first statements are detected. A row
without a balance does not break the chain: the next balance is compared with the last known one
plus every amount in between. After a mismatch the actual balance becomes the new anchor so one
gap does not make every later row look wrong.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from bank_analyzer.database.repositories import (
    AccountRepository,
    ImportRepository,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.models import AccountType, BatchStatus, CanonicalTransaction
from bank_analyzer.domain.money import Money

_LIABILITY = frozenset({AccountType.CREDIT_CARD, AccountType.LOAN})


class ReconStatus(StrEnum):
    NOT_AVAILABLE = "not_available"
    OK = "ok"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class Mismatch:
    transaction_id: str
    row_index: int
    location: str | None
    expected_minor: int
    actual_minor: int

    @property
    def delta_minor(self) -> int:
        """Actual minus expected balance.

        For a deposit account this equals the cash-flow amount of whatever is unaccounted for:
        -200.00 means a withdrawal of 200.00 may be missing, +200.00 means a deposit may be missing
        or a withdrawal was counted twice. (For a card or loan the sign is inverted.)
        """
        return self.actual_minor - self.expected_minor


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: ReconStatus
    currency: str | None
    steps_checked: int
    mismatch_count: int
    orientation: str | None  # "listed" or "newest_first"
    first_mismatch: Mismatch | None
    opening_check: str | None  # None when no opening balance was given, else "ok"/"mismatch"
    closing_check: str | None
    notes: tuple[str, ...]


def source_order_key(txn: CanonicalTransaction) -> tuple[int, int]:
    """Position of a row in its source file: (page number, row number within the table)."""
    location = txn.source_ref.location or ""
    page = (
        int(location.split()[-1])
        if location.startswith("page ") and location.split()[-1].isdigit()
        else 0
    )
    return (page, txn.source_ref.row_index)


def _effect(amount_minor: int, liability: bool) -> int:
    return -amount_minor if liability else amount_minor


@dataclass(slots=True)
class _Pass:
    steps: int = 0
    mismatches: int = 0
    first: Mismatch | None = None
    last_balance: int | None = None  # expected closing figure after the last row


def _walk(rows: Sequence[CanonicalTransaction], liability: bool, opening: int | None) -> _Pass:
    result = _Pass()
    anchor = opening
    running = 0
    for txn in rows:
        running += _effect(txn.amount_minor, liability)
        actual = txn.reported_balance_minor
        if actual is None:
            continue
        if anchor is not None:
            result.steps += 1
            expected = anchor + running
            if expected != actual:
                result.mismatches += 1
                if result.first is None:
                    result.first = Mismatch(
                        txn.id,
                        txn.source_ref.row_index,
                        txn.source_ref.location,
                        expected,
                        actual,
                    )
        anchor, running = actual, 0
    result.last_balance = None if anchor is None else anchor + running
    return result


def reconcile(
    rows_in_file_order: Sequence[CanonicalTransaction],
    account_type: AccountType,
    *,
    opening_balance_minor: int | None = None,
    closing_balance_minor: int | None = None,
) -> ReconciliationResult:
    """Reconcile one statement's rows (in the order they appear in the file)."""
    rows = list(rows_in_file_order)
    currencies = {t.currency for t in rows}
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    notes: list[str] = []
    if len(currencies) > 1:
        return ReconciliationResult(
            ReconStatus.NOT_AVAILABLE,
            None,
            0,
            0,
            None,
            None,
            None,
            None,
            ("The rows are in more than one currency; reconcile each currency separately.",),
        )
    liability = account_type in _LIABILITY
    with_balance = [t for t in rows if t.reported_balance_minor is not None]
    if not with_balance or (len(with_balance) < 2 and opening_balance_minor is None):
        return ReconciliationResult(
            ReconStatus.NOT_AVAILABLE,
            currency,
            0,
            0,
            None,
            None,
            None,
            None,
            ("The statement does not show enough running balances to check it.",),
        )

    listed = _walk(rows, liability, opening_balance_minor)
    chosen, ordered, orientation = listed, rows, "listed"
    if listed.mismatches:
        # A newest-first statement reads backwards. Date order is the reversed row order, and the
        # opening balance sits before the oldest row, i.e. the first row of the reversed list.
        backwards = _walk(rows[::-1], liability, opening_balance_minor)
        if backwards.mismatches < listed.mismatches:
            chosen, ordered, orientation = backwards, rows[::-1], "newest_first"
            notes.append("The statement is listed newest first; it was checked in date order.")

    opening_check = None
    if opening_balance_minor is not None:
        first_with = next(t for t in ordered if t.reported_balance_minor is not None)
        up_to_first = sum(
            _effect(t.amount_minor, liability) for t in ordered[: ordered.index(first_with) + 1]
        )
        opening_check = (
            "ok"
            if opening_balance_minor + up_to_first == first_with.reported_balance_minor
            else "mismatch"
        )

    closing_check = None
    if closing_balance_minor is not None:
        closing_check = "ok" if chosen.last_balance == closing_balance_minor else "mismatch"

    mismatch = chosen.first
    if mismatch is not None and currency is not None:
        gap = Money(abs(mismatch.delta_minor), currency).format()
        notes.append(
            f"The balance at row {mismatch.row_index} differs from the rows by {gap}. "
            "Transactions of that size may be missing or duplicated; none were guessed."
        )

    failed = chosen.mismatches > 0 or opening_check == "mismatch" or closing_check == "mismatch"
    return ReconciliationResult(
        ReconStatus.MISMATCH if failed else ReconStatus.OK,
        currency,
        chosen.steps,
        chosen.mismatches,
        orientation,
        mismatch,
        opening_check,
        closing_check,
        tuple(notes),
    )


@dataclass(frozen=True, slots=True)
class BatchReconciliation:
    batch_id: str
    created_at: str
    result: ReconciliationResult


def reconcile_account(
    conn: sqlite3.Connection, account_id: str, *, opening_balance_minor: int | None = None
) -> list[BatchReconciliation]:
    """Reconcile each committed import of an account separately (one import = one statement).

    ``opening_balance_minor`` is the balance before the account's first statement, so it is
    applied to the earliest import only; later statements carry on from there and are checked
    on their own running balances.
    """
    account = AccountRepository(conn).get(account_id)  # NotFoundError if unknown
    txns = TransactionRepository(conn)
    batches = [
        b
        for b in sorted(ImportRepository(conn).list_batches(), key=lambda b: b.created_at)
        if b.account_id == account_id and b.status == BatchStatus.COMMITTED
    ]
    results = []
    for position, batch in enumerate(batches):
        rows = sorted(txns.fetch_all(TransactionFilter(batch_id=batch.id)), key=source_order_key)
        opening = opening_balance_minor if position == 0 else None
        results.append(
            BatchReconciliation(
                batch.id,
                batch.created_at,
                reconcile(rows, account.account_type, opening_balance_minor=opening),
            )
        )
    return results
