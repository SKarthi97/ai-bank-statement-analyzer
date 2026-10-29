from __future__ import annotations

import pytest

from bank_analyzer.ai.response_validator import Verdict, validate_classification

ALLOWED = ["Groceries", "Dining", "Salary"]


def item(
    i: object, category: object = "Groceries", needs_review: object = False, reason: object = ""
) -> dict:
    return {"i": i, "category": category, "needs_review": needs_review, "reason": reason}


def check(raw: object, requested: list[int] | None = None):  # noqa: ANN201
    return validate_classification(raw, requested=requested or [0], allowed=ALLOWED)


class TestAccepted:
    def test_valid_item(self) -> None:
        out = check({"results": [item(0, "Dining", False, "looks like a cafe")]})
        o = out.items[0]
        assert (o.verdict, o.category, o.reason) == (
            Verdict.ACCEPTED,
            "Dining",
            "looks like a cafe",
        )
        assert not out.structural_error and out.unexpected_items == 0

    def test_all_requested_items_answered(self) -> None:
        out = check({"results": [item(0), item(1, "Dining"), item(2, "Salary")]}, [0, 1, 2])
        assert [out.items[i].category for i in range(3)] == ["Groceries", "Dining", "Salary"]
        assert out.count(Verdict.ACCEPTED) == 3

    def test_answer_order_does_not_matter(self) -> None:
        out = check({"results": [item(1, "Dining"), item(0)]}, [0, 1])
        assert out.items[0].category == "Groceries" and out.items[1].category == "Dining"


class TestAbstained:
    def test_needs_review_flag(self) -> None:
        o = check({"results": [item(0, "Groceries", True)]}).items[0]
        assert o.verdict == Verdict.ABSTAINED and o.category is None

    def test_needs_review_category(self) -> None:
        o = check({"results": [item(0, "NEEDS_REVIEW", False)]}).items[0]
        assert o.verdict == Verdict.ABSTAINED and o.category is None

    def test_flag_wins_over_a_category(self) -> None:
        # "I picked Salary but I'm not sure": no category may be taken from an unsure answer
        assert check({"results": [item(0, "Salary", True)]}).items[0].category is None


class TestInvalidItems:
    @pytest.mark.parametrize(
        "category", ["Rent", "groceries", "GROCERIES", "Groceries ", "", "Other"]
    )
    def test_category_must_match_an_allowed_name_exactly(self, category: str) -> None:
        o = check({"results": [item(0, category)]}).items[0]
        assert o.verdict == Verdict.INVALID and o.category is None

    @pytest.mark.parametrize(
        "bad",
        [
            {"i": 0, "category": "Groceries"},  # missing fields
            {"i": "0", "category": "Groceries", "needs_review": False, "reason": ""},  # wrong type
            {
                "i": True,
                "category": "Groceries",
                "needs_review": False,
                "reason": "",
            },  # bool as int
            {"i": 0, "category": 5, "needs_review": False, "reason": ""},
            {"i": 0, "category": "Groceries", "needs_review": "no", "reason": ""},
            {"i": 0, "category": "Groceries", "needs_review": False, "reason": "", "extra": 1},
            {"i": 0.0, "category": "Groceries", "needs_review": False, "reason": ""},
            "not a dict",
            None,
            42,
        ],
    )
    def test_malformed_item_is_ignored_and_the_row_stays_unanswered(self, bad: object) -> None:
        out = check({"results": [bad]})
        assert out.items[0].verdict == Verdict.INVALID
        assert out.unexpected_items == 1

    def test_missing_item_is_invalid(self) -> None:
        out = check({"results": [item(0)]}, [0, 1])
        assert out.items[1].verdict == Verdict.INVALID

    def test_duplicate_answers_are_never_resolved_by_picking_one(self) -> None:
        out = check({"results": [item(0, "Groceries"), item(0, "Salary")]})
        assert out.items[0].verdict == Verdict.INVALID

    def test_unrequested_index_is_ignored(self) -> None:
        out = check({"results": [item(0), item(99, "Salary")]})
        assert 99 not in out.items and out.unexpected_items == 1

    def test_one_bad_item_does_not_spoil_the_others(self) -> None:
        out = check({"results": [item(0), item(1, "Bogus"), item(2, "Dining")]}, [0, 1, 2])
        assert [out.items[i].verdict for i in range(3)] == [
            Verdict.ACCEPTED,
            Verdict.INVALID,
            Verdict.ACCEPTED,
        ]


class TestStructuralFailures:
    @pytest.mark.parametrize(
        "raw",
        [
            None,
            [],
            "text",
            42,
            {},
            {"results": "not a list"},
            {"results": None},
            {"results": [], "extra": 1},
            {"result": []},
            [item(0)],
            {"results": {"i": 0}},
        ],
    )
    def test_wrong_shape_invalidates_everything(self, raw: object) -> None:
        out = check(raw, [0, 1])
        assert out.structural_error
        assert all(o.verdict == Verdict.INVALID for o in out.items.values())
        assert set(out.items) == {0, 1}


class TestReasonText:
    def test_reason_is_sanitised_and_capped(self) -> None:
        o = check({"results": [item(0, "Dining", False, "a\x00b\n<script>​" + "x" * 500)]}).items[0]
        assert "\x00" not in o.reason and "\n" not in o.reason and "​" not in o.reason
        assert len(o.reason) <= 120

    def test_reason_is_kept_as_inert_text(self) -> None:
        o = check({"results": [item(0, "Dining", False, "<b>ignore previous</b>")]}).items[0]
        assert o.reason == "<b>ignore previous</b>"  # stored as text; the UI must escape it
