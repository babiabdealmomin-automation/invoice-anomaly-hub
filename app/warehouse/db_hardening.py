"""
Database hardening, safe DataFrame validation, and composite indexing utilities
for Invoice Spend & Anomaly Intelligence Hub.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence
import pandas as pd
from sqlalchemy import event, text
from sqlalchemy.engine import Engine


def configure_sqlite_wal_and_timeout(engine: Engine, timeout_seconds: float = 30.0) -> Engine:
    """
    Configure SQLite connection timeout (e.g., 30.0s) and enforce WAL mode
    (PRAGMA journal_mode=WAL) on every new DB-API connection to prevent
    'database is locked' errors under concurrent Streamlit sessions.
    """
    if not str(engine.url).startswith("sqlite"):
        return engine

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)};")
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute("PRAGMA synchronous = NORMAL;")
            cursor.execute("PRAGMA foreign_keys = ON;")
        finally:
            cursor.close()

    # Trigger an immediate pragma check on the engine
    with engine.begin() as conn:
        conn.execute(text(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)};"))
        conn.execute(text("PRAGMA journal_mode = WAL;"))
        conn.execute(text("PRAGMA synchronous = NORMAL;"))
        conn.execute(text("PRAGMA foreign_keys = ON;"))

    return engine


def ensure_composite_audit_indexes(engine: Engine) -> None:
    """
    Create composite indexes on fact_invoices for frequent analytical queries
    filtering by source/origin, date, and anomaly/risk status.
    Inspects existing columns dynamically so it remains safe across schema revisions.
    """
    with engine.begin() as conn:
        cols_rows = conn.execute(text("PRAGMA table_info(fact_invoices);")).fetchall()
        existing_cols = {str(row[1]).lower() for row in cols_rows}
        if not existing_cols:
            return

        # Identify best matching columns for source/origin, date, and anomaly/risk status
        origin_col = next(
            (c for c in ("source_filename", "origin", "vendor_key", "vendor_name") if c in existing_cols),
            None,
        )
        date_col = next(
            (c for c in ("invoice_date", "date_key", "created_at") if c in existing_cols),
            None,
        )
        anomaly_col = next(
            (c for c in ("risk_level", "anomaly_status", "risk_score") if c in existing_cols),
            None,
        )

        index_cols = [c for c in (origin_col, date_col, anomaly_col) if c is not None]
        if len(index_cols) >= 2:
            cols_sql = ", ".join(index_cols)
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS idx_fact_invoices_origin_date_anomaly "
                    f"ON fact_invoices ({cols_sql});"
                )
            )

        if "file_sha256" in existing_cols:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_fact_invoices_sha256 "
                    "ON fact_invoices (file_sha256);"
                )
            )

        # Ensure Audit Disposition columns exist on fact_invoices (safe schema migration)
        if "review_status" not in existing_cols:
            conn.execute(
                text(
                    "ALTER TABLE fact_invoices "
                    "ADD COLUMN review_status TEXT NOT NULL DEFAULT 'PENDING';"
                )
            )
        if "reviewer_note" not in existing_cols:
            conn.execute(
                text(
                    "ALTER TABLE fact_invoices "
                    "ADD COLUMN reviewer_note TEXT DEFAULT '';"
                )
            )

        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_fact_invoices_review_status "
                "ON fact_invoices (review_status);"
            )
        )


def update_invoice_disposition(
    engine: Engine,
    invoice_key: int,
    review_status: str,
    reviewer_note: str = "",
) -> None:
    """
    Persist the accountant's audit disposition decision (PENDING, APPROVED, FLAGGED, DISMISSED)
    and justification note for a specific invoice in fact_invoices.
    """
    allowed_statuses = {"PENDING", "APPROVED", "FLAGGED", "DISMISSED"}
    clean_status = review_status.strip().upper()
    if clean_status not in allowed_statuses:
        raise ValueError(f"Unsupported review_status: {review_status}")

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE fact_invoices
                SET review_status = :status,
                    reviewer_note = :note
                WHERE invoice_key = :inv_key;
                """
            ),
            {
                "status": clean_status,
                "note": reviewer_note.strip(),
                "inv_key": int(invoice_key),
            },
        )


def fetch_invoice_dispositions_map(engine: Engine) -> Dict[int, Dict[str, str]]:
    """Return a lookup dictionary mapping invoice_key -> {review_status, reviewer_note}."""
    with engine.begin() as conn:
        cols_rows = conn.execute(text("PRAGMA table_info(fact_invoices);")).fetchall()
        existing_cols = {str(row[1]).lower() for row in cols_rows}
        if "invoice_key" not in existing_cols or "review_status" not in existing_cols:
            return {}
        rows = conn.execute(
            text(
                """
                SELECT invoice_key,
                       COALESCE(NULLIF(TRIM(review_status), ''), 'PENDING') AS review_status,
                       COALESCE(reviewer_note, '') AS reviewer_note
                FROM fact_invoices;
                """
            )
        ).fetchall()
    return {
        int(r[0]): {
            "review_status": str(r[1]).upper(),
            "reviewer_note": str(r[2] or ""),
        }
        for r in rows
        if r[0] is not None
    }