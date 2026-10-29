"""Possible duplicate charges among stored transactions.

Two charges are *candidates* when they share account, currency, amount and normalized description
and fall within a few days of each other. Candidates are only ever shown: nothing is deleted or
changed. Legitimate repeats exist (two coffees, a habitual daily purchase), so each pair carries
context for the user to judge:

* ``same_import`` - both rows came from the same file (often genuinely repeated lines);
* ``chain_length`` - how many consecutive look-alikes form the run. A long run is a habit, not a
  duplicate charge, and the anomaly rules skip it.

Import-time matching against already-stored rows lives in ``import_service``; this module looks
at what is already stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from bank_analyzer.domain.models import CanonicalTransaction

DEFAULT_WINDOW_DAYS = 2


@dataclass(frozen=True, slots=True)
class DuplicatePair:
    first_id: str
    second_id: str  # the later transaction
    days_apart: int
    same_import: bool
    chain_length: int  # consecutive look-alikes (within the window) this pair belongs to


def find_duplicate_pairs(
    txns: Sequence[CanonicalTransaction], *, window_days: int = DEFAULT_WINDOW_DAYS
) -> list[DuplicatePair]:
    """Consecutive look-alike pairs within ``window_days``, oldest first."""
    groups: dict[tuple[str, str, int, str], list[CanonicalTransaction]] = {}
    for txn in txns:
        if txn.is_voided or txn.amount_minor == 0:
            continue
        key = (txn.account_id, txn.currency, txn.amount_minor, txn.normalized_description)
        groups.setdefault(key, []).append(txn)

    pairs: list[DuplicatePair] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda t: (t.txn_date, t.created_at, t.id))
        # split into runs where each neighbour is within the window
        runs: list[list[CanonicalTransaction]] = [[group[0]]]
        for previous, current in zip(group, group[1:], strict=False):
            if (current.txn_date - previous.txn_date).days <= window_days:
                runs[-1].append(current)
            else:
                runs.append([current])
        for run in runs:
            for previous, current in zip(run, run[1:], strict=False):
                pairs.append(
                    DuplicatePair(
                        first_id=previous.id,
                        second_id=current.id,
                        days_apart=(current.txn_date - previous.txn_date).days,
                        same_import=previous.import_batch_id == current.import_batch_id,
                        chain_length=len(run),
                    )
                )
    return sorted(pairs, key=lambda p: (p.second_id, p.first_id))
