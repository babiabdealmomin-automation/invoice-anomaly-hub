"""
Vendor Behavioral Profiling Engine (Star Schema Analytical Layer).
Analyzes longitudinal vendor transaction history across fact_invoices and
dim_vendors to compute:
- Historical average invoice total (historical_avg_invoice)
- Percentage of round-number invoices (round_amount_ratio_pct)
- Percentage of month-end invoices issued in the last 3 days of the month (month_end_ratio_pct)
- VENDOR_VOLUME_SPIKE detection when a new invoice exceeds N multiples
  (default 2.5x) of the vendor's historical average invoice amount.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


class VendorBehaviorProfile(BaseModel):
    """Longitudinal behavioral metrics computed for a single vendor."""

    vendor_name: str
    invoice_count: int = Field(default=0)
    historical_avg_invoice: float = Field(default=0.0)
    round_amount_count: int = Field(default=0)
    round_amount_ratio_pct: float = Field(default=0.0)
    month_end_count: int = Field(default=0)
    month_end_ratio_pct: float = Field(default=0.0)


class VendorVolumeSpikeResult(BaseModel):
    """Outcome of comparing a current invoice against the vendor's behavioral profile."""

    triggered: bool = Field(default=False)
    rule_code: str = Field(default="VENDOR_VOLUME_SPIKE")
    vendor_name: str = Field(default="")
    historical_invoice_count: int = Field(default=0)
    historical_avg_invoice: float = Field(default=0.0)
    current_invoice_amount: float = Field(default=0.0)
    spike_multiple: float = Field(default=0.0)
    threshold_multiple: float = Field(default=2.5)
    round_amount_ratio_pct: float = Field(default=0.0)
    month_end_ratio_pct: float = Field(default=0.0)


def _parse_date_safe(val: Any) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    txt = str(val).strip()
    if not txt:
        return None
    try:
        return date.fromisoformat(txt[:10])
    except ValueError:
        return None


def _is_month_end_last_3_days(dt: Optional[date]) -> bool:
    if dt is None:
        return False
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    return dt.day >= (last_day - 2)


def _is_round_invoice_amount(amount: float) -> bool:
    if amount <= 0:
        return False
    # Exact multiple of 500 or 1000 with zero fractional cents
    return round(amount, 2) % 500 == 0


def _query_vendor_historical_rows(
    conn_or_session: Any,
    vendor_name: Optional[str] = None,
    exclude_invoice_key: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Star Schema query joining fact_invoices with dim_vendors and dim_dates
    (with safe fallback to fact_invoices columns if schema is minimal).
    """
    inspect_sql = text("PRAGMA table_info(fact_invoices);")
    cols_rows = conn_or_session.execute(inspect_sql).fetchall()
    fact_cols = {str(r[1]).lower() for r in cols_rows}
    if not fact_cols:
        return []

    has_dim_vendors = (
        conn_or_session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='dim_vendors';")
        ).fetchone()
        is not None
    )
    has_dim_dates = (
        conn_or_session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='dim_dates';")
        ).fetchone()
        is not None
    )

    if has_dim_vendors and "vendor_key" in fact_cols:
        vendor_expr = "COALESCE(dv.vendor_name, fi.vendor_name, 'Unknown')"
        join_vendor = "LEFT JOIN dim_vendors dv ON fi.vendor_key = dv.vendor_key"
    else:
        vendor_expr = "COALESCE(fi.vendor_name, 'Unknown')"
        join_vendor = ""

    if has_dim_dates and "date_key" in fact_cols:
        date_expr = "COALESCE(fi.invoice_date, dd.full_date)"
        join_date = "LEFT JOIN dim_dates dd ON fi.date_key = dd.date_key"
    elif "invoice_date" in fact_cols:
        date_expr = "fi.invoice_date"
        join_date = ""
    else:
        date_expr = "fi.created_at"
        join_date = ""

    where_clauses = ["fi.grand_total_amount IS NOT NULL", "fi.grand_total_amount > 0"]
    params: Dict[str, Any] = {}

    if vendor_name:
        where_clauses.append(f"LOWER(TRIM({vendor_expr})) = LOWER(TRIM(:v_name))")
        params["v_name"] = vendor_name

    if exclude_invoice_key is not None and "invoice_key" in fact_cols:
        where_clauses.append("fi.invoice_key != :excl_key")
        params["excl_key"] = int(exclude_invoice_key)

    sql = text(
        f"""
        SELECT
            {vendor_expr} AS vendor_name,
            fi.grand_total_amount AS grand_total_amount,
            {date_expr} AS invoice_date
        FROM fact_invoices fi
        {join_vendor}
        {join_date}
        WHERE {' AND '.join(where_clauses)}
        """
    )
    rows = conn_or_session.execute(sql, params).fetchall()
    return [
        {
            "vendor_name": str(r[0] or "Unknown").strip(),
            "grand_total_amount": float(r[1] or 0.0),
            "invoice_date": r[2],
        }
        for r in rows
    ]


def compute_vendor_behavior_profile(
    conn_or_session: Any,
    vendor_name: str,
    exclude_invoice_key: Optional[int] = None,
) -> VendorBehaviorProfile:
    """Compute longitudinal behavioral metrics for a specific vendor from the Star Schema."""
    rows = _query_vendor_historical_rows(
        conn_or_session,
        vendor_name=vendor_name,
        exclude_invoice_key=exclude_invoice_key,
    )
    count = len(rows)
    if count == 0:
        return VendorBehaviorProfile(vendor_name=vendor_name)

    amounts = [r["grand_total_amount"] for r in rows]
    avg_amt = round(sum(amounts) / count, 2)
    round_cnt = sum(1 for a in amounts if _is_round_invoice_amount(a))
    month_end_cnt = sum(
        1 for r in rows if _is_month_end_last_3_days(_parse_date_safe(r["invoice_date"]))
    )

    return VendorBehaviorProfile(
        vendor_name=vendor_name,
        invoice_count=count,
        historical_avg_invoice=avg_amt,
        round_amount_count=round_cnt,
        round_amount_ratio_pct=round((round_cnt / count) * 100.0, 1),
        month_end_count=month_end_cnt,
        month_end_ratio_pct=round((month_end_cnt / count) * 100.0, 1),
    )


def evaluate_vendor_volume_spike(
    conn_or_session: Any,
    vendor_name: str,
    current_invoice_amount: float,
    exclude_invoice_key: Optional[int] = None,
    min_historical_invoices: int = 3,
    spike_multiplier: float = 2.5,
) -> VendorVolumeSpikeResult:
    """
    Detect VENDOR_VOLUME_SPIKE when a vendor with established history
    (default >= 3 prior invoices) submits an invoice exceeding `spike_multiplier`
    (default 2.5x) of their historical average invoice amount.
    """
    profile = compute_vendor_behavior_profile(
        conn_or_session=conn_or_session,
        vendor_name=vendor_name,
        exclude_invoice_key=exclude_invoice_key,
    )
    if (
        profile.invoice_count < min_historical_invoices
        or profile.historical_avg_invoice <= 0.0
        or current_invoice_amount <= 0.0
    ):
        return VendorVolumeSpikeResult(
            triggered=False,
            vendor_name=vendor_name,
            historical_invoice_count=profile.invoice_count,
            historical_avg_invoice=profile.historical_avg_invoice,
            current_invoice_amount=current_invoice_amount,
            threshold_multiple=spike_multiplier,
            round_amount_ratio_pct=profile.round_amount_ratio_pct,
            month_end_ratio_pct=profile.month_end_ratio_pct,
        )

    multiple = round(current_invoice_amount / profile.historical_avg_invoice, 2)
    triggered = multiple >= spike_multiplier

    return VendorVolumeSpikeResult(
        triggered=triggered,
        vendor_name=vendor_name,
        historical_invoice_count=profile.invoice_count,
        historical_avg_invoice=profile.historical_avg_invoice,
        current_invoice_amount=current_invoice_amount,
        spike_multiple=multiple,
        threshold_multiple=spike_multiplier,
        round_amount_ratio_pct=profile.round_amount_ratio_pct,
        month_end_ratio_pct=profile.month_end_ratio_pct,
    )


def fetch_all_vendor_profiles_dataframe(engine: Engine) -> pd.DataFrame:
    """
    Return an analytical DataFrame summarizing behavioral metrics for every
    vendor in the Star Schema warehouse:
    - vendor_name
    - invoice_count
    - historical_avg_invoice
    - round_amount_ratio_pct
    - month_end_ratio_pct
    """
    columns = [
        "vendor_name",
        "invoice_count",
        "historical_avg_invoice",
        "round_amount_ratio_pct",
        "month_end_ratio_pct",
    ]
    with engine.begin() as conn:
        rows = _query_vendor_historical_rows(conn)

    if not rows:
        return pd.DataFrame(columns=columns)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["vendor_name"], []).append(r)

    records: List[Dict[str, Any]] = []
    for v_name, v_rows in grouped.items():
        cnt = len(v_rows)
        avg_val = round(sum(x["grand_total_amount"] for x in v_rows) / cnt, 2)
        round_cnt = sum(1 for x in v_rows if _is_round_invoice_amount(x["grand_total_amount"]))
        me_cnt = sum(
            1 for x in v_rows if _is_month_end_last_3_days(_parse_date_safe(x["invoice_date"]))
        )
        records.append(
            {
                "vendor_name": v_name,
                "invoice_count": cnt,
                "historical_avg_invoice": avg_val,
                "round_amount_ratio_pct": round((round_cnt / cnt) * 100.0, 1),
                "month_end_ratio_pct": round((me_cnt / cnt) * 100.0, 1),
            }
        )

    df = pd.DataFrame(records, columns=columns)
    return df.sort_values(by=["invoice_count", "historical_avg_invoice"], ascending=False).reset_index(
        drop=True
    )