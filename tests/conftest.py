from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from bank_analyzer.database.migrations import open_database
from bank_analyzer.database.repositories import (
    AccountRepository,
    AuditRepository,
    CategoryRepository,
    ImportRepository,
    TransactionRepository,
)
from bank_analyzer.domain.models import Account, AccountType


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = open_database(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def audit(conn: sqlite3.Connection) -> AuditRepository:
    return AuditRepository(conn)


@pytest.fixture
def accounts(conn: sqlite3.Connection) -> AccountRepository:
    return AccountRepository(conn)


@pytest.fixture
def categories(conn: sqlite3.Connection) -> CategoryRepository:
    return CategoryRepository(conn)


@pytest.fixture
def txns(conn: sqlite3.Connection) -> TransactionRepository:
    return TransactionRepository(conn)


@pytest.fixture
def imports(conn: sqlite3.Connection) -> ImportRepository:
    return ImportRepository(conn)


@pytest.fixture
def usd_account(accounts: AccountRepository) -> Account:
    return accounts.create("Checking", AccountType.DEPOSIT, "USD")
