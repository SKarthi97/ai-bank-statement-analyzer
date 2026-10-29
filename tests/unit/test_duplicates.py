from __future__ import annotations

import copy

import pytest

from analytics_data import make
from bank_analyzer.services.duplicates import find_duplicate_pairs


def pairs(*txns, window: int = 2):  # noqa: ANN002, ANN201
    return find_duplicate_pairs(list(txns), window_days=window)


class TestDetection:
    def test_same_charge_within_the_window(self) -> None:
        a = make("2024-03-01", "CITY GYM", "-29.99")
        b = make("2024-03-02", "CITY GYM", "-29.99")
        (pair,) = pairs(a, b)
        assert (pair.first_id, pair.second_id, pair.days_apart) == (a.id, b.id, 1)
        assert pair.chain_length == 2

    @pytest.mark.parametrize(("gap", "found"), [(0, True), (2, True), (3, False)])
    def test_window_boundary(self, gap: int, found: bool) -> None:
        a = make("2024-03-01", "CITY GYM", "-29.99")
        b = make(f"2024-03-{1 + gap:02d}", "CITY GYM", "-29.99")
        assert bool(pairs(a, b)) is found

    @pytest.mark.parametrize(
        "other",
        [
            {"amount": "-30.00"},
            {"desc": "OTHER GYM"},
            {"account": "B"},
            {"currency": "EUR"},
        ],
    )
    def test_any_difference_means_no_pair(self, other: dict) -> None:
        base = {"desc": "CITY GYM", "amount": "-29.99"}
        a = make("2024-03-01", base["desc"], base["amount"])
        b = make(
            "2024-03-01",
            other.get("desc", base["desc"]),
            other.get("amount", base["amount"]),
            account=other.get("account", "A"),
            currency=other.get("currency", "USD"),
        )
        assert pairs(a, b) == []

    def test_zero_amounts_and_voided_rows_are_ignored(self) -> None:
        assert pairs(make("2024-03-01", "X", "0.00"), make("2024-03-01", "X", "0.00")) == []
        a, b = make("2024-03-01", "X", "-5.00"), make("2024-03-01", "X", "-5.00", voided=True)
        assert pairs(a, b) == []

    def test_reference_numbers_in_the_description_do_not_hide_a_duplicate(self) -> None:
        a = make("2024-03-01", "CITY GYM REF 1234567", "-29.99")
        b = make("2024-03-02", "CITY GYM REF 7654321", "-29.99")
        assert len(pairs(a, b)) == 1  # compared on the normalized description

    def test_same_import_is_reported_for_context(self) -> None:
        a = make("2024-03-01", "COFFEE", "-3.00", batch="one")
        b = make("2024-03-01", "COFFEE", "-3.00", batch="one")
        c = make("2024-03-02", "COFFEE", "-3.00", batch="two")
        # which of two same-day rows is "first" is arbitrary, so identify pairs as sets
        by_ids = {frozenset((p.first_id, p.second_id)): p for p in pairs(a, b, c)}
        assert by_ids[frozenset((a.id, b.id))].same_import is True
        cross = [p for ids, p in by_ids.items() if c.id in ids]
        assert cross and all(p.same_import is False for p in cross)


class TestRuns:
    def test_a_daily_habit_forms_one_long_run(self) -> None:
        days = [make(f"2024-03-{d:02d}", "MORNING COFFEE", "-3.50") for d in range(1, 7)]
        result = pairs(*days)
        assert len(result) == 5 and {p.chain_length for p in result} == {6}

    def test_separate_bursts_are_separate_runs(self) -> None:
        burst1 = [make("2024-03-01", "X", "-9.00"), make("2024-03-02", "X", "-9.00")]
        burst2 = [make("2024-03-20", "X", "-9.00"), make("2024-03-21", "X", "-9.00")]
        result = pairs(*burst1, *burst2)
        assert len(result) == 2 and {p.chain_length for p in result} == {2}

    def test_three_in_a_row(self) -> None:
        rows = [make(f"2024-03-0{d}", "X", "-9.00") for d in (1, 2, 3)]
        assert [p.chain_length for p in pairs(*rows)] == [3, 3]

    def test_result_is_deterministic_regardless_of_input_order(self) -> None:
        rows = [make(f"2024-03-0{d}", "X", "-9.00") for d in (1, 2, 3)]
        forward, backward = pairs(*rows), pairs(*reversed(rows))
        assert [(p.first_id, p.second_id) for p in forward] == [
            (p.first_id, p.second_id) for p in backward
        ]


def test_finding_duplicates_never_changes_or_removes_anything() -> None:
    rows = [make("2024-03-01", "X", "-9.00"), make("2024-03-01", "X", "-9.00")]
    before = copy.deepcopy([r.model_dump() for r in rows])
    pairs(*rows)
    assert [r.model_dump() for r in rows] == before and len(rows) == 2
