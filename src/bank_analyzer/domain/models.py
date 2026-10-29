"""Persisted entities: accounts, categories, canonical transactions, import batches.

Models are frozen Pydantic objects so validated data cannot be mutated after creation.
Amounts are integer minor units; see ``money.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from bank_analyzer.domain.money import Money

CURRENCY_PATTERN = r"^[A-Z]{3}$"
MAX_DESCRIPTION_LENGTH = 1000


def new_id() -> str:
    """A new random UUID4 string."""
    return str(uuid.uuid4())


def utc_now_iso() -> str:
    """Timezone-aware UTC timestamp, ISO-8601 with a trailing Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AccountType(StrEnum):
    DEPOSIT = "deposit"
    CREDIT_CARD = "credit_card"
    LOAN = "loan"
    CASH = "cash"
    INVESTMENT = "investment"


class CategoryKind(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    NEUTRAL = "neutral"


class TxnType(StrEnum):
    PURCHASE = "purchase"
    INCOME = "income"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    CASH_WITHDRAWAL = "cash_withdrawal"
    REFUND = "refund"
    REVERSAL = "reversal"
    FEE = "fee"
    INTEREST = "interest"
    LOAN_PAYMENT = "loan_payment"
    CARD_PAYMENT = "card_payment"
    OTHER = "other"


class CategorySource(StrEnum):
    NONE = "none"
    USER_MANUAL = "user_manual"
    USER_RULE = "user_rule"
    MERCHANT_MAPPING = "merchant_mapping"
    BUILTIN_RULE = "builtin_rule"
    LLM = "llm"


class ReviewStatus(StrEnum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED_SUGGESTION = "rejected_suggestion"


class MatchType(StrEnum):
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    REGEX = "regex"
    MERCHANT_KEY = "merchant_key"


class AmountSign(StrEnum):
    IN = "in"
    OUT = "out"


class FileKind(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"
    PDF = "pdf"
    PDF_OCR = "pdf_ocr"


class BatchStatus(StrEnum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


# Default categories: (name, kind). Kind drives report grouping only; the amount's sign is the
# source of truth for direction. Cash withdrawals and refunds are neutral because they are
# reported separately from expenses/income to avoid double counting (docs/05, section 2).
DEFAULT_CATEGORIES: tuple[tuple[str, CategoryKind], ...] = (
    ("Income", CategoryKind.INCOME),
    ("Salary", CategoryKind.INCOME),
    ("Groceries", CategoryKind.EXPENSE),
    ("Dining", CategoryKind.EXPENSE),
    ("Housing", CategoryKind.EXPENSE),
    ("Utilities", CategoryKind.EXPENSE),
    ("Transport", CategoryKind.EXPENSE),
    ("Fuel", CategoryKind.EXPENSE),
    ("Healthcare", CategoryKind.EXPENSE),
    ("Education", CategoryKind.EXPENSE),
    ("Shopping", CategoryKind.EXPENSE),
    ("Entertainment", CategoryKind.EXPENSE),
    ("Subscriptions", CategoryKind.EXPENSE),
    ("Insurance", CategoryKind.EXPENSE),
    ("Taxes and fees", CategoryKind.EXPENSE),
    ("Loan payments", CategoryKind.EXPENSE),
    ("Transfers", CategoryKind.TRANSFER),
    ("Cash withdrawals", CategoryKind.NEUTRAL),
    ("Refunds", CategoryKind.NEUTRAL),
    ("Savings and investments", CategoryKind.NEUTRAL),
    ("Other", CategoryKind.EXPENSE),
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Account(_Frozen):
    """A local account alias. Full account numbers are never stored."""

    id: str
    alias: str = Field(min_length=1, max_length=100)
    account_type: AccountType
    currency: str = Field(pattern=CURRENCY_PATTERN)
    last4: str | None = Field(default=None, pattern=r"^[0-9A-Za-z]{4}$")
    opening_balance_minor: StrictInt | None = None
    is_active: bool = True
    created_at: str


class Category(_Frozen):
    id: str
    name: str = Field(min_length=1, max_length=100)
    kind: CategoryKind
    is_active: bool = True
    is_default: bool = False
    merged_into_id: str | None = None
    created_at: str


class SourceRef(_Frozen):
    """Where a transaction came from, for traceability."""

    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    location: str | None = None
    row_index: int = Field(ge=0)


class CanonicalTransaction(_Frozen):
    """One normalized statement line."""

    id: str
    account_id: str
    import_batch_id: str
    txn_date: date
    booking_date: date | None = None
    original_description: str = Field(max_length=MAX_DESCRIPTION_LENGTH)
    normalized_description: str = Field(max_length=MAX_DESCRIPTION_LENGTH)
    merchant_key: str | None = None
    amount_minor: StrictInt
    currency: str = Field(pattern=CURRENCY_PATTERN)
    reported_balance_minor: StrictInt | None = None
    txn_type: TxnType = TxnType.OTHER
    category_id: str | None = None
    category_source: CategorySource = CategorySource.NONE
    review_status: ReviewStatus = ReviewStatus.NEEDS_REVIEW
    transfer_group_id: str | None = None
    source_ref: SourceRef
    occurrence_index: int = Field(default=0, ge=0)
    is_voided: bool = False
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if (self.category_id is None) != (self.category_source == CategorySource.NONE):
            raise ValueError("category_id must be set exactly when category_source is not 'none'")
        if (
            self.category_source == CategorySource.USER_MANUAL
            and self.review_status != ReviewStatus.APPROVED
        ):
            raise ValueError("a user-set category must be approved")
        return self

    @property
    def amount(self) -> Money:
        """The signed amount as Money."""
        return Money(self.amount_minor, self.currency)

    @property
    def reported_balance(self) -> Money | None:
        if self.reported_balance_minor is None:
            return None
        return Money(self.reported_balance_minor, self.currency)


class ImportBatch(_Frozen):
    """One file import event; the unit of rollback."""

    id: str
    account_id: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_kind: FileKind
    display_name: str | None = Field(default=None, max_length=200)
    row_count_read: int = Field(ge=0)
    row_count_imported: int = Field(ge=0)
    row_count_skipped: int = Field(ge=0)
    warnings: tuple[str, ...] = ()
    status: BatchStatus = BatchStatus.COMMITTED
    created_at: str
    rolled_back_at: str | None = None


class ImportSummary(_Frozen):
    batch_id: str
    imported: int
    skipped: int
    warnings: tuple[str, ...] = ()


class RollbackSummary(_Frozen):
    batch_id: str
    voided: int


class CategorizationRule(_Frozen):
    """A user-defined rule. Lower ``priority`` runs first; the first match wins."""

    id: str
    priority: int
    match_type: MatchType
    pattern: str = Field(min_length=1, max_length=200)
    amount_sign: AmountSign | None = None
    category_id: str
    is_active: bool = True
    created_at: str


class MerchantMapping(_Frozen):
    """A merchant the user has approved for a category."""

    merchant_key: str
    category_id: str
    approved_at: str
