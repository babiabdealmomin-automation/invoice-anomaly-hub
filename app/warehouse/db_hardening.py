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


def upsert_sqlite_on_conflict(
    engine: Engine,
    table_name: str,
    record: Dict[str, Any],
    conflict_columns: Sequence[str],
    update_columns: Optional[Sequence[str]] = None,
) -> None:
    """
    Perform a safe SQLite UPSERT using:
        INSERT INTO ... VALUES (...) ON CONFLICT(...) DO UPDATE SET ...
    instead of INSERT OR REPLACE, preserving existing row IDs and foreign key
    relationships in child tables while updating only target fields.
    """
    if not record:
        return

    all_cols = list(record.keys())
    target_update_cols = (
        list(update_columns)
        if update_columns is not None
        else [c for c in all_cols if c not in conflict_columns]
    )

    cols_clause = ", ".join(all_cols)
    placeholders_clause = ", ".join(f":{c}" for c in all_cols)
    conflict_clause = ", ".join(conflict_columns)

    if target_update_cols:
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in target_update_cols)
        sql = (
            f"INSERT INTO {table_name} ({cols_clause}) "
            f"VALUES ({placeholders_clause}) "
            f"ON CONFLICT({conflict_clause}) DO UPDATE SET {set_clause};"
        )
    else:
        sql = (
            f"INSERT INTO {table_name} ({cols_clause}) "
            f"VALUES ({placeholders_clause}) "
            f"ON CONFLICT({conflict_clause}) DO NOTHING;"
        )

    with engine.begin() as conn:
        conn.execute(text(sql), record)


def normalize_and_validate_dataframe(
    df: Optional[pd.DataFrame],
    required_columns: Iterable[str] = (),
    numeric_columns: Iterable[str] = (),
) -> pd.DataFrame:
    """
    Normalize DataFrame column casing/whitespace, validate required columns
    to prevent KeyError, and coerce numeric columns WITHOUT blindly replacing
    missing values (NaN) with 0.0 so genuine zeros remain distinguishable
    from missing data.
    """
    if df is None:
        return pd.DataFrame(columns=list(required_columns))

    clean_df = df.copy()
    clean_df.columns = [str(col).strip().lower() for col in clean_df.columns]

    for col in required_columns:
        col_lower = col.strip().lower()
        if col_lower not in clean_df.columns:
            clean_df[col_lower] = pd.NA

    for num_col in numeric_columns:
        col_lower = num_col.strip().lower()
        if col_lower in clean_df.columns:
            # Coerce invalid strings to NaN/NA while preserving missingness (do NOT fillna(0.0))
            clean_df[col_lower] = pd.to_numeric(clean_df[col_lower], errors="coerce")

    return clean_df