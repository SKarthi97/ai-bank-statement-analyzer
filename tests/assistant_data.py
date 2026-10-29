"""A small statement history for assistant tests, with hand-computed totals.

Today is 2024-03-20, so "last month" is February 2024.

  Jan  income 3000.00 | groceries 200.00 + 50.00, housing 1000.00, subscriptions 9.99
       expenses 1259.99
  Feb  income 3000.00 | groceries 300.00, housing 1000.00, dining 40.00, subscriptions 9.99,
       uncategorized BOOKSHOP 25.00 twice (a duplicate candidate, one day apart)
       expenses 1399.99
  Mar  income 3000.00 | groceries 100.00, subscriptions 14.99 (the streaming price went up 50%)
       expenses 114.99

  Feb vs Jan expenses: +140.00 = +11.1% (140.00 / 1259.99 = 11.11%)
"""

from __future__ import annotations

import sqlite3
from datetime import date

from bank_analyzer.domain.models import Account
from fakes import load_transactions

TODAY = date(2024, 3, 20)

SPECS = [
    {"desc": "ACME PAYROLL", "amount": 300000, "day": "2024-01-02", "category": "Salary"},
    {"desc": "FRESH MART", "amount": -20000, "day": "2024-01-05", "category": "Groceries"},
    {"desc": "CITY LANDLORD RENT", "amount": -100000, "day": "2024-01-06", "category": "Housing"},
    {"desc": "STREAMLY", "amount": -999, "day": "2024-01-10", "category": "Subscriptions"},
    {"desc": "FRESH MART", "amount": -5000, "day": "2024-01-20", "category": "Groceries"},
    {"desc": "ACME PAYROLL", "amount": 300000, "day": "2024-02-02", "category": "Salary"},
    {"desc": "FRESH MART", "amount": -30000, "day": "2024-02-04", "category": "Groceries"},
    {"desc": "CITY LANDLORD RENT", "amount": -100000, "day": "2024-02-06", "category": "Housing"},
    {"desc": "STREAMLY", "amount": -999, "day": "2024-02-10", "category": "Subscriptions"},
    {"desc": "CAFE LUNA", "amount": -4000, "day": "2024-02-10", "category": "Dining"},
    {"desc": "BOOKSHOP", "amount": -2500, "day": "2024-02-12"},
    {"desc": "BOOKSHOP", "amount": -2500, "day": "2024-02-13"},
    {"desc": "ACME PAYROLL", "amount": 300000, "day": "2024-03-02", "category": "Salary"},
    {"desc": "FRESH MART", "amount": -10000, "day": "2024-03-03", "category": "Groceries"},
    {"desc": "STREAMLY", "amount": -1499, "day": "2024-03-10", "category": "Subscriptions"},
]


def load_history(conn: sqlite3.Connection, account: Account) -> None:
    load_transactions(conn, account.id, SPECS)


def read_only(conn: sqlite3.Connection) -> sqlite3.Connection:
    """The same in-memory database, now refusing writes (stands in for ``connect_readonly``)."""
    conn.execute("PRAGMA query_only = ON")
    return conn
