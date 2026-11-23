from datetime import timedelta
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.intelligence.audit_engine import evaluate_invoice_intelligence
from app.schemas import ExtractedInvoice, IntelligenceVerdict
from app.warehouse.models import Base, DimDate, DimVendor, FactInvoice, FactInvoiceLine


def init_schema(engine) -> None:
    Base.metadata.create_all(bind=engine)


def reset_warehouse(engine) -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _get_or_create_vendor(session: Session, invoice: ExtractedInvoice) -> tuple[DimVendor, str | None]:
    canonical = invoice.vendor_name.strip()
    vendor = session.execute(select(DimVendor).where(DimVendor.canonical_name == canonical)).scalar_one_or_none()
    if vendor is None:
        vendor = DimVendor(
            canonical_name=canonical,
            tax_identifier=invoice.tax_identifier,
            default_currency=invoice.currency.upper(),
        )
        session.add(vendor)
        session.flush()
        return vendor, None

    prev_nif = vendor.tax_identifier
    if not vendor.tax_identifier and invoice.tax_identifier:
        vendor.tax_identifier = invoice.tax_identifier
    return vendor, prev_nif


def _get_or_create_date(session: Session, dt) -> DimDate:
    d_key = int(dt.strftime("%Y%m%d"))
    dim_dt = session.get(DimDate, d_key)
    if dim_dt is None:
        quarter = ((dt.month - 1) // 3) + 1
        dim_dt = DimDate(
            date_key=d_key,
            full_date=dt,
            year=dt.year,
            quarter=quarter,
            month=dt.month,
            day=dt.day,
        )
        session.add(dim_dt)
        session.flush()
    return dim_dt


def _historical_avg_prices(session: Session, vendor_key: int) -> dict[str, float]:
    rows = session.execute(
        select(
            FactInvoiceLine.normalized_item,
            func.avg(FactInvoiceLine.unit_price),
            func.count(FactInvoiceLine.line_key),
        )
        .join(FactInvoice, FactInvoice.invoice_key == FactInvoiceLine.invoice_key)
        .where(FactInvoice.vendor_key == vendor_key)
        .group_by(FactInvoiceLine.normalized_item)
        .having(func.count(FactInvoiceLine.line_key) >= 1)
    ).all()
    return {item: float(avg_p) for item, avg_p, _ in rows if avg_p is not None}


def _check_near_duplicate(session: Session, vendor_key: int, invoice: ExtractedInvoice, window_days: int = 7) -> bool:
    start_dt = invoice.issue_date - timedelta(days=window_days)
    end_dt = invoice.issue_date + timedelta(days=window_days)
    existing = session.execute(
        select(FactInvoice.invoice_key)
        .join(DimDate, DimDate.date_key == FactInvoice.date_key)
        .where(
            FactInvoice.vendor_key == vendor_key,
            func.abs(FactInvoice.grand_total_amount - float(invoice.grand_total_amount)) <= 0.01,
            DimDate.full_date >= start_dt,
            DimDate.full_date <= end_dt,
        )
        .limit(1)
    ).first()
    return existing is not None


def _recent_vendor_totals(session: Session, vendor_key: int, invoice: ExtractedInvoice, window_days: int = 7) -> list[float]:
    start_dt = invoice.issue_date - timedelta(days=window_days)
    end_dt = invoice.issue_date + timedelta(days=window_days)
    rows = session.execute(
        select(FactInvoice.grand_total_amount)
        .join(DimDate, DimDate.date_key == FactInvoice.date_key)
        .where(
            FactInvoice.vendor_key == vendor_key,
            DimDate.full_date >= start_dt,
            DimDate.full_date <= end_dt,
        )
    ).all()
    return [float(r[0]) for r in rows if r[0] is not None]


def ingest_invoice_record(
    session: Session,
    invoice: ExtractedInvoice,
    lines: list[dict],
    file_sha256: str,
    source_filename: str,
    price_spike_threshold_pct: float = 25.0,
    near_duplicate_days: int = 7,
    procurement_threshold: float = 3_500_000.0,
    rule_weights: dict[str, int] | None = None,
) -> dict:
    dup = session.execute(select(FactInvoice).where(FactInvoice.file_sha256 == file_sha256)).scalar_one_or_none()
    if dup is not None:
        return {
            "status": "DUPLICATE",
            "invoice_key": dup.invoice_key,
            "risk_score": dup.risk_score,
            "risk_level": dup.risk_level,
            "message": "Exact file hash already exists in warehouse.",
        }

    vendor, known_nif = _get_or_create_vendor(session, invoice)
    dim_date = _get_or_create_date(session, invoice.issue_date)
    hist_prices = _historical_avg_prices(session, vendor.vendor_key)
    near_dup = _check_near_duplicate(session, vendor.vendor_key, invoice, window_days=near_duplicate_days)
    recent_totals = _recent_vendor_totals(session, vendor.vendor_key, invoice, window_days=near_duplicate_days)

    verdict: IntelligenceVerdict = evaluate_invoice_intelligence(
        invoice=invoice,
        lines=lines,
        historical_item_prices=hist_prices,
        has_near_duplicate=near_dup,
        price_spike_threshold_pct=price_spike_threshold_pct,
        known_vendor_nif=known_nif,
        split_invoice_candidates=recent_totals,
        procurement_threshold=procurement_threshold,
        rule_weights=rule_weights,
    )

    fact = FactInvoice(
        vendor_key=vendor.vendor_key,
        date_key=dim_date.date_key,
        invoice_number=invoice.invoice_number,
        file_sha256=file_sha256,
        source_filename=source_filename,
        currency=invoice.currency.upper(),
        subtotal_amount=float(invoice.subtotal_amount),
        tax_amount=float(invoice.tax_amount),
        discount_amount=float(invoice.discount_amount),
        grand_total_amount=float(invoice.grand_total_amount),
        expected_total=verdict.expected_total,
        discrepancy_amount=verdict.discrepancy_amount,
        audit_status=verdict.audit_status,
        risk_score=verdict.risk_score,
        risk_level=verdict.risk_level,
        risk_flags=",".join(verdict.risk_flags),
        summary_notes=verdict.summary_notes,
    )
    session.add(fact)
    session.flush()

    for ln in verdict.lines:
        session.add(
            FactInvoiceLine(
                invoice_key=fact.invoice_key,
                line_number=ln.line_number,
                item_description=ln.item_description,
                normalized_item=ln.normalized_item,
                quantity=ln.quantity,
                unit_price=ln.unit_price,
                line_total=ln.line_total,
                expected_line_total=ln.expected_line_total,
                is_line_valid=1 if ln.is_line_valid else 0,
                historical_avg_price=ln.historical_avg_price,
                price_deviation_pct=ln.price_deviation_pct,
                is_price_anomaly=1 if ln.is_price_anomaly else 0,
            )
        )

    session.commit()
    return {
        "status": verdict.audit_status,
        "invoice_key": fact.invoice_key,
        "risk_score": verdict.risk_score,
        "risk_level": verdict.risk_level,
        "risk_flags": verdict.risk_flags,
        "discrepancy_amount": verdict.discrepancy_amount,
        "summary_notes": verdict.summary_notes,
    }


def fetch_invoices_dataframe(engine) -> pd.DataFrame:
    query = """
    SELECT
        fi.invoice_key,
        fi.invoice_number,
        dv.canonical_name AS vendor_name,
        dd.full_date AS issue_date,
        fi.currency,
        fi.subtotal_amount,
        fi.tax_amount,
        fi.discount_amount,
        fi.grand_total_amount,
        fi.expected_total,
        fi.discrepancy_amount,
        fi.audit_status,
        fi.risk_score,
        fi.risk_level,
        fi.risk_flags,
        fi.summary_notes,
        fi.source_filename
    FROM fact_invoices fi
    JOIN dim_vendors dv ON dv.vendor_key = fi.vendor_key
    JOIN dim_dates dd ON dd.date_key = fi.date_key
    ORDER BY fi.invoice_key DESC
    """
    return pd.read_sql(query, engine)


def fetch_vendor_spend_dataframe(engine) -> pd.DataFrame:
    query = """
    SELECT
        dv.canonical_name AS vendor_name,
        COUNT(fi.invoice_key) AS invoice_count,
        SUM(fi.grand_total_amount) AS total_spend,
        SUM(ABS(fi.discrepancy_amount)) AS total_discrepancy,
        ROUND(AVG(fi.risk_score), 1) AS avg_risk_score
    FROM fact_invoices fi
    JOIN dim_vendors dv ON dv.vendor_key = fi.vendor_key
    GROUP BY dv.vendor_key, dv.canonical_name
    ORDER BY total_spend DESC
    """
    return pd.read_sql(query, engine)


def fetch_lines_for_invoice(engine, invoice_key: int) -> pd.DataFrame:
    query = """
    SELECT
        line_number,
        item_description,
        quantity,
        unit_price,
        line_total,
        expected_line_total,
        is_line_valid,
        historical_avg_price,
        price_deviation_pct,
        is_price_anomaly
    FROM fact_invoice_lines
    WHERE invoice_key = :inv_key
    ORDER BY line_number ASC
    """
    return pd.read_sql(query, engine, params={"inv_key": int(invoice_key)})
