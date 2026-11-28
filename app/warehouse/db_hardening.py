"""
Database hardening, safe DataFrame validation, and composite indexing utilities
for Invoice Spend & Anomaly Intelligence Hub.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence
import pandas as pd
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.security.crypto_vault import (
    decrypt_sensitive_text,
    encrypt_sensitive_text,
)


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
    workspace_slug: str = "default",
) -> None:
    """
    Persist the accountant's audit disposition decision (PENDING, APPROVED, FLAGGED, DISMISSED)
    and encrypt the justification note at rest (Fernet AES+HMAC) before saving in SQLite.
    """
    allowed_statuses = {"PENDING", "APPROVED", "FLAGGED", "DISMISSED"}
    clean_status = review_status.strip().upper()
    if clean_status not in allowed_statuses:
        raise ValueError(f"Unsupported review_status: {review_status}")

    encrypted_note = encrypt_sensitive_text(
        reviewer_note.strip(),
        workspace_slug=workspace_slug,
    )

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
                "note": encrypted_note,
                "inv_key": int(invoice_key),
            },
        )


def fetch_invoice_dispositions_map(
    engine: Engine,
    workspace_slug: str = "default",
) -> Dict[int, Dict[str, str]]:
    """Return a lookup dictionary mapping invoice_key -> {review_status, decrypted reviewer_note}."""
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
            "reviewer_note": decrypt_sensitive_text(
                str(r[2] or ""),
                workspace_slug=workspace_slug,
            ),
        }
        for r in rows
        if r[0] is not None
    }


def normalize_and_validate_dataframe(
    df: pd.DataFrame,
    required_columns: Optional[Sequence[str]] = None,
    numeric_columns: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """
    Normalize column names to lowercase, ensure required columns exist
    (preventing KeyError), and coerce numeric columns cleanly without turning
    missing values (NaN) into 0.0 so real zeros are never confused with missing data.
    """
    if df is None or df.empty:
        cols = [c.strip().lower() for c in (required_columns or [])]
        return pd.DataFrame(columns=cols)

    clean_df = df.copy()
    clean_df.columns = [str(c).strip().lower() for c in clean_df.columns]

    for col in required_columns or []:
        norm_col = str(col).strip().lower()
        if norm_col not in clean_df.columns:
            clean_df[norm_col] = pd.NA

    for num_col in numeric_columns or []:
        norm_num = str(num_col).strip().lower()
        if norm_num in clean_df.columns:
            clean_df[norm_num] = pd.to_numeric(clean_df[norm_num], errors="coerce")

    return clean_df


def upsert_sqlite_on_conflict(
    engine: Engine,
    table_name: str,
    records: Iterable[Dict[str, Any]],
    conflict_columns: Sequence[str],
    update_columns: Optional[Sequence[str]] = None,
) -> int:
    """
    Perform atomic SQLite UPSERT using `ON CONFLICT(...) DO UPDATE SET ...`
    instead of `INSERT OR REPLACE` so primary keys and foreign-key child rows
    are preserved cleanly without delete+reinsert side effects.
    """
    rows_list = list(records)
    if not rows_list:
        return 0

    all_cols = list(rows_list[0].keys())
    conflict_cols = [c for c in conflict_columns if c in all_cols]
    if not conflict_cols:
        raise ValueError("conflict_columns must be present in records.")

    cols_to_update = (
        [c for c in update_columns if c in all_cols and c not in conflict_cols]
        if update_columns is not None
        else [c for c in all_cols if c not in conflict_cols]
    )

    insert_cols_sql = ", ".join(all_cols)
    placeholders_sql = ", ".join(f":{c}" for c in all_cols)
    conflict_sql = ", ".join(conflict_cols)

    if cols_to_update:
        set_sql = ", ".join(f"{c} = excluded.{c}" for c in cols_to_update)
        sql = text(
            f"INSERT INTO {table_name} ({insert_cols_sql}) VALUES ({placeholders_sql}) "
            f"ON CONFLICT ({conflict_sql}) DO UPDATE SET {set_sql};"
        )
    else:
        sql = text(
            f"INSERT INTO {table_name} ({insert_cols_sql}) VALUES ({placeholders_sql}) "
            f"ON CONFLICT ({conflict_sql}) DO NOTHING;"
        )

    with engine.begin() as conn:
        for row in rows_list:
            conn.execute(sql, row)

    return len(rows_list)