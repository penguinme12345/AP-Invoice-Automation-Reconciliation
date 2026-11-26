"""SQLite helpers. Swap the connect() function for Postgres in production."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(read_sql("schema.sql"))
    conn.executescript(read_sql("views.sql"))
    conn.commit()


def reset_ap_tables(conn: sqlite3.Connection) -> None:
    """Clear the AP side (invoices, matches, tickets) but keep ERP master data."""
    for t in ("payments", "approved_for_payment", "exception_tickets", "match_results",
              "invoice_lines", "invoices", "pipeline_runs"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('invoices','pipeline_runs')")
    conn.commit()
