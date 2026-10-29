from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from bank_analyzer.domain.models import (
    CanonicalTransaction,
    CategorySource,
    ReviewStatus,
    SourceRef,
    new_id,
    utc_now_iso,
)
from bank_analyzer.domain.money import Money
from bank_analyzer.domain.validation import (
    W_EMPTY_DESCRIPTION,
    W_FUTURE_DATE,
    W_ZERO_AMOUNT,
    sanitize_text,
    validate_transaction,
)


def make(**overrides: Any) -> CanonicalTransaction:
    base: dict[str, Any] = {
        "id": new_id(),
        "account_id": "a",
        "import_batch_id": "b",
        "txn_date": date(2024, 1, 2),
        "original_description": "Coffee",
        "normalized_description": "COFFEE",
        "amount_minor": -450,
        "currency": "USD",
        "source_ref": SourceRef(file_sha256="0" * 64, row_index=1),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    base.update(overrides)
    return CanonicalTransaction(**base)


def test_amount_property_is_money() -> None:
    assert make().amount == Money(-450, "USD")


def test_float_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        make(amount_minor=4.5)


def test_numeric_string_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        make(amount_minor="450")


def test_bad_currency_rejected() -> None:
    with pytest.raises(ValidationError):
        make(currency="usd")


def test_model_is_frozen() -> None:
    txn = make()
    with pytest.raises(ValidationError):
        txn.amount_minor = 1  # type: ignore[misc]


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        make(account_number="12345678")


def test_category_requires_source() -> None:
    with pytest.raises(ValidationError):
        make(category_id="c1")  # source defaults to none
    with pytest.raises(ValidationError):
        make(category_source=CategorySource.LLM)  # no category id


def test_user_manual_must_be_approved() -> None:
    with pytest.raises(ValidationError):
        make(category_id="c1", category_source=CategorySource.USER_MANUAL)
    ok = make(
        category_id="c1",
        category_source=CategorySource.USER_MANUAL,
        review_status=ReviewStatus.APPROVED,
    )
    assert ok.category_id == "c1"


def test_description_length_capped() -> None:
    with pytest.raises(ValidationError):
        make(original_description="x" * 1001)


def test_timestamps_are_utc_iso() -> None:
    assert utc_now_iso().endswith("Z")


class TestSanitize:
    def test_strips_control_characters_and_collapses_whitespace(self) -> None:
        text, truncated = sanitize_text("A\x00B\t\nC ​D")
        assert text == "A B C D"
        assert not truncated

    def test_truncates(self) -> None:
        text, truncated = sanitize_text("x" * 5000)
        assert len(text) == 1000 and truncated


class TestTransactionValidation:
    def test_zero_amount_and_empty_description_warn(self) -> None:
        issues = validate_transaction(
            make(amount_minor=0, original_description=""), row_index=3, today=date(2024, 6, 1)
        )
        assert {i.code for i in issues} == {W_ZERO_AMOUNT, W_EMPTY_DESCRIPTION}
        assert all(i.row_index == 3 for i in issues)

    def test_future_date_warns(self) -> None:
        issues = validate_transaction(
            make(txn_date=date(2030, 1, 1)), row_index=1, today=date(2024, 6, 1)
        )
        assert [i.code for i in issues] == [W_FUTURE_DATE]

    def test_clean_row_has_no_issues(self) -> None:
        assert validate_transaction(make(), row_index=1, today=date(2024, 6, 1)) == []
