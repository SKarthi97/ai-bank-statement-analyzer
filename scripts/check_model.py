"""Check the local model and confirm the core app works without it.

Usage: ``python scripts/check_model.py``

Reports whether Ollama is reachable, whether the configured model is installed, and whether the
model returns valid structured output on five invented descriptions. It also exercises the
deterministic categorizer on a throw-away in-memory database to show that rule-based
categorization does not depend on the model. Exit code 0 means the application is usable; the
model being unavailable is reported, not treated as a failure.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bank_analyzer.ai.local_llm import LLMError, OllamaClient  # noqa: E402
from bank_analyzer.ai.selftest import run_selftest  # noqa: E402
from bank_analyzer.config import load_settings  # noqa: E402
from bank_analyzer.database.migrations import open_database  # noqa: E402
from bank_analyzer.database.repositories import (  # noqa: E402
    AccountRepository,
    CategoryRepository,
    ImportRepository,
)
from bank_analyzer.domain.errors import ConfigError  # noqa: E402
from bank_analyzer.domain.models import (  # noqa: E402
    AccountType,
    CanonicalTransaction,
    FileKind,
    ImportBatch,
    SourceRef,
    new_id,
    utc_now_iso,
)
from bank_analyzer.services.categorization import Categorizer  # noqa: E402


def core_features_without_model() -> bool:
    """Import a synthetic row and categorize it by rules only (no model involved)."""
    conn = open_database(":memory:")
    try:
        account = AccountRepository(conn).create("check", AccountType.DEPOSIT, "USD")
        now = utc_now_iso()
        batch = ImportBatch(
            id=new_id(),
            account_id=account.id,
            file_sha256="0" * 64,
            file_kind=FileKind.CSV,
            row_count_read=1,
            row_count_imported=1,
            row_count_skipped=0,
            created_at=now,
        )
        txn = CanonicalTransaction(
            id=new_id(),
            account_id=account.id,
            import_batch_id=batch.id,
            txn_date=date(2024, 1, 1),
            original_description="NETFLIX.COM",
            normalized_description="NETFLIX COM",
            merchant_key="NETFLIX",
            amount_minor=-1599,
            currency="USD",
            source_ref=SourceRef(file_sha256="0" * 64, row_index=1),
            created_at=now,
            updated_at=now,
        )
        ImportRepository(conn).commit_import(batch, (txn,))
        report = Categorizer(conn, llm=None).run()
        return report.by_source.get("builtin_rule") == 1
    finally:
        conn.close()


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration problem: {exc}")
        return 1
    print(f"Model server : {settings.ollama_url}")
    print(f"Model        : {settings.model}")

    client = OllamaClient(settings.ollama_url, settings.model, timeout_s=settings.llm_timeout_s)
    try:
        installed = client.list_models()
        print(f"Server       : reachable, {len(installed)} model(s) installed")
        model_ok = client.is_available()
        print(
            f"Model status : {'installed' if model_ok else 'NOT installed (try: ollama pull ' + settings.model + ')'}"
        )
    except LLMError as exc:
        print(f"Server       : not reachable ({exc})")
        model_ok = False

    if model_ok:
        conn = open_database(":memory:")
        try:
            names = [c.name for c in CategoryRepository(conn).list()]
        finally:
            conn.close()
        result = run_selftest(client, names)
        print(
            f"Self-test    : {'PASSED' if result.usable else 'FAILED'} ({result.answered}/{result.total}) {result.detail}"
        )

    works = core_features_without_model()
    print(
        f"Core features without a model (import + rule categorization): {'OK' if works else 'FAILED'}"
    )
    if not model_ok:
        print(
            "AI categorization is unavailable; everything else works, and rules still categorize."
        )
    return 0 if works else 1


if __name__ == "__main__":
    raise SystemExit(main())
