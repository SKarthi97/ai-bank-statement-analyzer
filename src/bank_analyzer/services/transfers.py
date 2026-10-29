"""Matching the two legs of a transfer between the user's own accounts.

A transfer between your accounts is not income or spending; counting both legs would inflate both.
But linking two unrelated transactions that merely share an amount and a date (rent out of one
account, salary into another) would hide real income or spending, so matching is deliberately
conservative:

* a *confident* pair is the only candidate for each other (1-to-1), AND at least one leg shows
  transfer evidence (a transfer category or transfer wording). Only these are linked automatically;
* a *suggested* pair is 1-to-1 but has no evidence: the user decides;
* an *ambiguous* group has several possible partners: the user decides which leg belongs where.

Nothing here changes amounts, categories or descriptions; a link only sets ``transfer_group_id``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bank_analyzer.database.repositories import (
    CategoryRepository,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.errors import InvalidStateError
from bank_analyzer.domain.models import CanonicalTransaction, Category, CategoryRole, new_id

DEFAULT_WINDOW_DAYS = 3
_HINTS = (
    "TRANSFER",
    "XFER",
    "OWN ACCOUNT",
    "INTERNAL",
    "CREDIT CARD PAYMENT",
    "CARD PAYMENT",
    "PAYMENT THANK YOU",
    "AUTOPAY",
)


@dataclass(frozen=True, slots=True)
class TransferPair:
    outflow_id: str
    inflow_id: str
    days_apart: int
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AmbiguousTransfer:
    """Several outflows and/or inflows that could pair up in more than one way."""

    outflow_ids: tuple[str, ...]
    inflow_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransferScan:
    confident: tuple[TransferPair, ...]
    suggested: tuple[TransferPair, ...]
    ambiguous: tuple[AmbiguousTransfer, ...]


def _evidence(
    out: CanonicalTransaction, inflow: CanonicalTransaction, categories: Mapping[str, Category]
) -> tuple[str, ...]:
    found: list[str] = []
    for txn in (out, inflow):
        category = categories.get(txn.category_id) if txn.category_id else None
        if category is not None and category.role == CategoryRole.TRANSFER:
            found.append("a leg is in a transfer category")
            break
    if any(h in t.normalized_description for t in (out, inflow) for h in _HINTS):
        found.append("the description mentions a transfer or card payment")
    return tuple(found)


def scan_transfers(
    txns: Sequence[CanonicalTransaction],
    categories: Mapping[str, Category],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> TransferScan:
    """Find candidate transfer pairs among live, not-yet-linked transactions."""
    eligible = [
        t for t in txns if not t.is_voided and t.transfer_group_id is None and t.amount_minor
    ]
    outflows = [t for t in eligible if t.amount_minor < 0]
    inflows_by_key: dict[tuple[str, int], list[CanonicalTransaction]] = {}
    for t in eligible:
        if t.amount_minor > 0:
            inflows_by_key.setdefault((t.currency, t.amount_minor), []).append(t)

    # bipartite candidate graph: outflow id -> set of inflow ids (and the reverse)
    out_to_in: dict[str, set[str]] = {}
    in_to_out: dict[str, set[str]] = {}
    by_id = {t.id: t for t in eligible}
    for out in outflows:
        for inflow in inflows_by_key.get((out.currency, -out.amount_minor), []):
            if (
                inflow.account_id != out.account_id
                and abs((inflow.txn_date - out.txn_date).days) <= window_days
            ):
                out_to_in.setdefault(out.id, set()).add(inflow.id)
                in_to_out.setdefault(inflow.id, set()).add(out.id)

    confident: list[TransferPair] = []
    suggested: list[TransferPair] = []
    ambiguous: list[AmbiguousTransfer] = []
    seen: set[str] = set()
    for out_id in sorted(out_to_in):
        if out_id in seen:
            continue
        # connected component of the candidate graph
        outs, ins, stack = {out_id}, set(), [("o", out_id)]
        while stack:
            side, node = stack.pop()
            neighbours = out_to_in[node] if side == "o" else in_to_out[node]
            for other in neighbours:
                if side == "o" and other not in ins:
                    ins.add(other)
                    stack.append(("i", other))
                elif side == "i" and other not in outs:
                    outs.add(other)
                    stack.append(("o", other))
        seen |= outs
        if len(outs) == 1 and len(ins) == 1:
            out, inflow = by_id[next(iter(outs))], by_id[next(iter(ins))]
            pair = TransferPair(
                out.id,
                inflow.id,
                abs((inflow.txn_date - out.txn_date).days),
                _evidence(out, inflow, categories),
            )
            (confident if pair.evidence else suggested).append(pair)
        else:
            ambiguous.append(AmbiguousTransfer(tuple(sorted(outs)), tuple(sorted(ins))))
    return TransferScan(tuple(confident), tuple(suggested), tuple(ambiguous))


class TransferService:
    """Applies and removes transfer links. Automatic linking is limited to confident pairs."""

    def __init__(self, conn: sqlite3.Connection, *, window_days: int = DEFAULT_WINDOW_DAYS) -> None:
        self._conn = conn
        self._txns = TransactionRepository(conn)
        self._window = window_days

    def scan(self) -> TransferScan:
        categories = {c.id: c for c in CategoryRepository(self._conn).list(active_only=False)}
        return scan_transfers(
            self._txns.fetch_all(TransactionFilter()), categories, window_days=self._window
        )

    def link_confident(self) -> int:
        """Link every confident pair. Returns the number of pairs linked."""
        scan = self.scan()
        for pair in scan.confident:
            self._txns.link_transfer(pair.outflow_id, pair.inflow_id, new_id())
        return len(scan.confident)

    def link(self, outflow_id: str, inflow_id: str) -> str:
        """Link two transactions the user says are one transfer. Returns the group id.

        Checks the legs are consistent (different accounts, same currency, equal opposite
        amounts) but, being the user's decision, does not require a date window or evidence.
        """
        out, inflow = self._txns.get(outflow_id), self._txns.get(inflow_id)
        if out.amount_minor >= 0 or inflow.amount_minor <= 0:
            raise InvalidStateError("a transfer needs one outflow and one inflow")
        if out.account_id == inflow.account_id:
            raise InvalidStateError("both legs are in the same account")
        if out.currency != inflow.currency or out.amount_minor != -inflow.amount_minor:
            raise InvalidStateError("the two legs must have the same amount and currency")
        group_id = new_id()
        self._txns.link_transfer(outflow_id, inflow_id, group_id)
        return group_id

    def unlink(self, group_id: str) -> int:
        """Release both legs of a link. Raises NotFoundError if there is no such link."""
        return self._txns.unlink_transfer(group_id)
