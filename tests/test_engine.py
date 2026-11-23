from datetime import date
from app.extraction.tabular_importer import parse_tabular_invoices
from app.intelligence.audit_engine import evaluate_invoice_intelligence
from app.schemas import ExtractedInvoice
from app.warehouse.models import build_engine, build_session_factory
from app.warehouse.repository import init_schema, ingest_invoice_record


def _sample_invoice(
    inv_num: str = "INV-001",
    dt: date = date(2026, 11, 15),
    price_2: float = 127.0,
    total: float | None = None,
    vendor: str = "شركة الأفق للتجارة",
    lines_override: list[dict] | None = None,
) -> tuple[ExtractedInvoice, list[dict]]:
    lines = lines_override if lines_override is not None else [
        {"line_number": 1, "item_description": "الخدمة الاولى", "quantity": 1, "unit_price": 123.0, "line_total": 123.0},
        {"line_number": 2, "item_description": "الخدمة الثانية", "quantity": 2, "unit_price": price_2, "line_total": round(2 * price_2, 2)},
        {"line_number": 3, "item_description": "الخدمة الثالثة", "quantity": 1, "unit_price": 123.0, "line_total": 123.0},
    ]
    computed_subtotal = round(sum(float(ln["line_total"]) for ln in lines), 2)
    final_total = computed_subtotal if total is None else total

    inv = ExtractedInvoice(
        vendor_name=vendor,
        tax_identifier="001916001234567",
        invoice_number=inv_num,
        issue_date=dt,
        currency="DZD",
        subtotal_amount=computed_subtotal,
        tax_amount=0.0,
        discount_amount=0.0,
        grand_total_amount=final_total,
    )
    return inv, lines


def test_clean_invoice_passes_with_low_risk():
    invoice, lines = _sample_invoice()
    verdict = evaluate_invoice_intelligence(invoice=invoice, lines=lines)
    assert verdict.audit_status == "PASSED"
    assert verdict.discrepancy_amount == 0.0
    assert verdict.risk_score == 0
    assert verdict.risk_level == "LOW"


def test_arithmetic_mismatch_flags_medium_risk():
    invoice, lines = _sample_invoice(total=540.0)
    verdict = evaluate_invoice_intelligence(invoice=invoice, lines=lines)
    assert verdict.audit_status == "FAILED"
    assert verdict.discrepancy_amount == 40.0
    assert "ARITHMETIC_MISMATCH" in verdict.risk_flags
    assert verdict.risk_score == 45
    assert verdict.risk_level == "MEDIUM"


def test_price_spike_detection_above_threshold():
    invoice, lines = _sample_invoice(price_2=200.0, total=646.0)
    verdict = evaluate_invoice_intelligence(
        invoice=invoice,
        lines=lines,
        historical_item_prices={"الخدمة الثانية": 100.0},
        price_spike_threshold_pct=25.0,
    )
    assert verdict.audit_status == "PASSED"
    assert "PRICE_SPIKE" in verdict.risk_flags
    assert verdict.lines[1].is_price_anomaly is True
    assert verdict.lines[1].price_deviation_pct == 100.0
    assert verdict.risk_score == 30


def test_warehouse_ingest_duplicate_and_near_duplicate():
    engine = build_engine("sqlite:///:memory:")
    init_schema(engine)
    SessionLocal = build_session_factory(engine)

    inv1, lines1 = _sample_invoice(inv_num="INV-100", dt=date(2026, 11, 10), total=500.0)
    inv2_near_dup, lines2 = _sample_invoice(inv_num="INV-101", dt=date(2026, 11, 13), total=500.0)

    with SessionLocal() as session:
        res1 = ingest_invoice_record(session, inv1, lines1, file_sha256="hash_aaa", source_filename="f1.pdf")
        assert res1["status"] == "PASSED"

        res_dup = ingest_invoice_record(session, inv1, lines1, file_sha256="hash_aaa", source_filename="f1.pdf")
        assert res_dup["status"] == "DUPLICATE"

        res_near = ingest_invoice_record(session, inv2_near_dup, lines2, file_sha256="hash_bbb", source_filename="f2.pdf")
        assert "NEAR_DUPLICATE_INVOICE" in res_near["risk_flags"]


def test_parse_csv_groups_lines_by_invoice():
    csv_data = (
        "vendor_name,invoice_number,issue_date,currency,subtotal_amount,tax_amount,discount_amount,grand_total_amount,line_number,item_description,quantity,unit_price,line_total\n"
        "شركة النور,INV-77,2026-11-01,DZD,300,0,0,300,1,طابعة,1,100,100\n"
        "شركة النور,INV-77,2026-11-01,DZD,300,0,0,300,2,حبر,2,100,200\n"
    ).encode("utf-8")

    batch = parse_tabular_invoices(csv_data, "sample.csv")
    assert len(batch) == 1
    inv, lines = batch[0]
    assert inv.invoice_number == "INV-77"
    assert len(lines) == 2
