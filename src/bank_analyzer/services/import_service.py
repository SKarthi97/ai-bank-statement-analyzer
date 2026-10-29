"""The import workflow: preview, then commit only after explicit confirmation.

This is the UI-agnostic service the Import Center page will call. It never saves anything during
``prepare_preview``. ``commit_preview`` refuses to proceed until the caller has acknowledged
every risk the preview found: rejected rows, low-confidence OCR amounts, a file that was already
imported, and suspected duplicates. Suspected duplicates are only *candidates*: the user chooses
which rows to skip, and nothing already stored is ever deleted or changed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from bank_analyzer.database.repositories import (
    AccountRepository,
    ImportRepository,
    TransactionRepository,
)
from bank_analyzer.domain.errors import InvalidStateError
from bank_analyzer.domain.imports import (
    ColumnMapping,
    ImportChoices,
    NormalizationResult,
    RawTable,
)
from bank_analyzer.domain.models import (
    ImportBatch,
    ImportSummary,
    new_id,
    utc_now_iso,
)
from bank_analyzer.security.file_security import safe_display_name
from bank_analyzer.services.normalization import normalize


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    """A new row that matches an already-stored transaction. The user decides what to do."""

    transaction_id: str  # the new (not yet saved) row
    existing_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """Everything the user must see before confirming. Nothing here has been saved."""

    raw: RawTable
    result: NormalizationResult
    batch: ImportBatch
    duplicates: tuple[DuplicateCandidate, ...]
    prior_imports: tuple[ImportBatch, ...]  # earlier committed imports of the same file bytes
    identical_rows_in_file: int  # legitimately repeated rows, kept and numbered

    @property
    def needs_rejected_ack(self) -> bool:
        return self.result.rows_rejected > 0

    @property
    def needs_low_confidence_ack(self) -> bool:
        return bool(self.result.low_confidence_rows)

    @property
    def needs_reimport_ack(self) -> bool:
        return bool(self.prior_imports)

    @property
    def needs_duplicate_decision(self) -> bool:
        return bool(self.duplicates)


def find_duplicate_candidates(
    txns: TransactionRepository, result: NormalizationResult
) -> tuple[DuplicateCandidate, ...]:
    """Match new rows against stored ones on (account, date, amount, currency, description).

    If the key already has E stored rows, the first E new rows with that key are flagged: the
    remainder are genuinely new (for example three identical coffees when two are stored). This
    keeps overlapping statements safe without treating legitimate repeats as duplicates.
    """
    seen: dict[tuple[str, date, int, str, str], int] = {}
    candidates: list[DuplicateCandidate] = []
    for txn in result.transactions:
        key = (
            txn.account_id,
            txn.txn_date,
            txn.amount_minor,
            txn.currency,
            txn.normalized_description,
        )
        existing = txns.find_matching(
            account_id=txn.account_id,
            txn_date=txn.txn_date,
            amount_minor=txn.amount_minor,
            currency=txn.currency,
            normalized_description=txn.normalized_description,
        )
        position = seen.get(key, 0)
        seen[key] = position + 1
        if position < len(existing):
            candidates.append(
                DuplicateCandidate(
                    transaction_id=txn.id,
                    existing_ids=(existing[position],),
                    reason="Same account, date, amount and description as a stored transaction.",
                )
            )
    return tuple(candidates)


def prepare_preview(
    conn: sqlite3.Connection,
    raw: RawTable,
    mapping: ColumnMapping,
    choices: ImportChoices,
    *,
    display_name: str | None = None,
    today: date | None = None,
    min_ocr_confidence: float = 80.0,
) -> ImportPreview:
    """Normalize and analyse a file without saving anything. Raises if the account is unknown."""
    AccountRepository(conn).get(choices.account_id)
    batch_id = new_id()
    result = normalize(
        raw,
        mapping,
        choices,
        import_batch_id=batch_id,
        today=today,
        min_ocr_confidence=min_ocr_confidence,
    )
    codes = [f"{code} x{count}" for code, count in result.issue_counts().items()]
    batch = ImportBatch(
        id=batch_id,
        account_id=choices.account_id,
        file_sha256=raw.file_sha256,
        file_kind=raw.file_kind,
        display_name=safe_display_name(display_name) if display_name else None,
        row_count_read=result.rows_read,
        row_count_imported=len(result.transactions),
        row_count_skipped=result.rows_skipped + result.rows_rejected,
        warnings=tuple(codes),
        created_at=utc_now_iso(),
    )
    return ImportPreview(
        raw=raw,
        result=result,
        batch=batch,
        duplicates=find_duplicate_candidates(TransactionRepository(conn), result),
        prior_imports=tuple(ImportRepository(conn).find_by_hash(raw.file_sha256)),
        identical_rows_in_file=sum(1 for t in result.transactions if t.occurrence_index > 0),
    )


def commit_preview(
    conn: sqlite3.Connection,
    preview: ImportPreview,
    *,
    confirmed: bool,
    skip_transaction_ids: Collection[str] = frozenset(),
    keep_remaining_duplicates: bool = False,
    accept_rejected_rows: bool = False,
    confirm_low_confidence_amounts: bool = False,
    allow_reimport: bool = False,
) -> ImportSummary:
    """Save the previewed import after explicit confirmation of every flagged risk.

    ``skip_transaction_ids`` are new rows the user chose not to import (typically duplicates).
    Any suspected duplicate that is neither skipped nor covered by ``keep_remaining_duplicates``
    blocks the commit. Raises InvalidStateError describing the first missing acknowledgement.
    """
    if not confirmed:
        raise InvalidStateError("The import was not confirmed.")
    if preview.needs_rejected_ack and not accept_rejected_rows:
        raise InvalidStateError(
            f"{preview.result.rows_rejected} row(s) were rejected and will not be imported. "
            "Acknowledge this to continue."
        )
    if preview.needs_low_confidence_ack and not confirm_low_confidence_amounts:
        raise InvalidStateError(
            f"{len(preview.result.low_confidence_rows)} row(s) have low OCR confidence. "
            "Confirm their amounts against the original to continue."
        )
    if preview.needs_reimport_ack and not allow_reimport:
        raise InvalidStateError(
            "This exact file was already imported. Allow a re-import explicitly to continue."
        )
    skip = set(skip_transaction_ids)
    unknown = skip - {t.id for t in preview.result.transactions}
    if unknown:
        raise InvalidStateError("A row chosen to skip is not part of this import.")
    undecided = [d for d in preview.duplicates if d.transaction_id not in skip]
    if undecided and not keep_remaining_duplicates:
        raise InvalidStateError(
            f"{len(undecided)} suspected duplicate(s) need a decision: skip them or keep them."
        )

    kept = tuple(t for t in preview.result.transactions if t.id not in skip)
    warnings = list(preview.batch.warnings)
    if skip:
        warnings.append(f"ROWS_SKIPPED_BY_USER x{len(skip)}")
    batch = preview.batch.model_copy(
        update={
            "row_count_skipped": preview.batch.row_count_skipped + len(skip),
            "warnings": tuple(warnings),
        }
    )
    return ImportRepository(conn).commit_import(batch, kept)
