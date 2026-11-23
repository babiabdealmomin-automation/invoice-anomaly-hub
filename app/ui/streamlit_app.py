from io import BytesIO
import pandas as pd
import streamlit as st
from app.config import settings
from app.extraction.ai_parser import parse_invoice_pdf_native
from app.extraction.pdf_reader import compute_sha256
from app.extraction.tabular_importer import parse_tabular_invoices
from app.intelligence.audit_engine import DEFAULT_RULE_WEIGHTS
from app.warehouse.models import build_engine, build_session_factory
from app.warehouse.repository import (
    fetch_invoices_dataframe,
    fetch_lines_for_invoice,
    fetch_vendor_spend_dataframe,
    init_schema,
    ingest_invoice_record,
    reset_warehouse,
)

st.set_page_config(page_title="Invoice Spend & Anomaly Hub", layout="wide")

engine = build_engine(settings.DATABASE_URL)
init_schema(engine)
SessionLocal = build_session_factory(engine)

st.title("Invoice Spend & Anomaly Intelligence Hub")
st.caption("Native PDF Document Intelligence • Deterministic Audit • Warehouse Exports")

RULE_LABELS: dict[str, str] = {
    "ARITHMETIC_MISMATCH": "Arithmetic Mismatch",
    "PRICE_SPIKE": "Price Spike",
    "NEAR_DUPLICATE_INVOICE": "Near-Duplicate Invoice",
    "SPLIT_INVOICE_PATTERN": "Split-Invoice Pattern",
    "HIGH_RISK_RED_FLAGS": "High-Risk Red Flags",
    "THIRD_PARTY_OR_OFFSHORE_PAYEE": "Third-Party / Offshore Payee",
    "NIF_MISMATCH": "NIF Mismatch",
    "BANK_ACCOUNT_CHANGED": "Bank Account Changed",
    "INVALID_NIF_FORMAT": "Invalid NIF Format",
    "ROUND_HIGH_VALUE_TOTAL": "Large Round Total",
    "NON_POSITIVE_GRAND_TOTAL": "Non-Positive Total",
}

with st.sidebar:
    st.header("Configuration")
    api_key_input = st.text_input("Gemini API Key", value=settings.GEMINI_API_KEY, type="password")
    spike_pct = st.slider("Price Spike Threshold (%)", min_value=5.0, max_value=100.0, value=settings.PRICE_SPIKE_THRESHOLD_PCT, step=5.0)
    dup_days = st.slider("Near-Duplicate & Split Window (Days)", min_value=1, max_value=30, value=settings.NEAR_DUPLICATE_DAYS, step=1)
    proc_threshold = st.number_input("Procurement Threshold (DZD)", min_value=10000.0, max_value=100000000.0, value=3500000.0, step=100000.0)

    st.divider()
    st.subheader("Warehouse Maintenance")
    if st.button("Reset Warehouse", type="secondary", use_container_width=True):
        reset_warehouse(engine)
        st.success("Warehouse cleared and reinitialized.")
        st.rerun()

    st.divider()
    st.subheader("Risk Rule Weights & Toggles")
    active_weights: dict[str, int] = {}
    for rule_key, default_w in DEFAULT_RULE_WEIGHTS.items():
        label = RULE_LABELS.get(rule_key, rule_key)
        enabled = st.checkbox(label, value=True, key=f"rule_en_{rule_key}")
        if enabled:
            w_val = st.slider(
                f"{label} Weight",
                min_value=0,
                max_value=60,
                value=int(default_w),
                step=5,
                key=f"rule_w_{rule_key}",
            )
            active_weights[rule_key] = int(w_val)
        else:
            active_weights[rule_key] = 0

st.subheader("1. Ingest Invoice (PDF / CSV / XLSX)")
uploaded = st.file_uploader("Upload invoice PDF, CSV, or XLSX", type=["pdf", "csv", "xlsx"])
if uploaded is not None and st.button("Analyze & Store in Warehouse", type="primary"):
    try:
        file_bytes = uploaded.getvalue()
        base_hash = compute_sha256(file_bytes)
        lower_name = uploaded.name.lower()

        results = []
        with SessionLocal() as session:
            if lower_name.endswith(".pdf"):
                invoice, lines = parse_invoice_pdf_native(file_bytes, api_key=api_key_input)
                res = ingest_invoice_record(
                    session=session,
                    invoice=invoice,
                    lines=lines,
                    file_sha256=base_hash,
                    source_filename=uploaded.name,
                    price_spike_threshold_pct=spike_pct,
                    near_duplicate_days=dup_days,
                    procurement_threshold=float(proc_threshold),
                    rule_weights=active_weights,
                )
                results.append(res)
            else:
                batch = parse_tabular_invoices(file_bytes, uploaded.name)
                for idx, (invoice, lines) in enumerate(batch, start=1):
                    row_hash = compute_sha256(f"{base_hash}:{idx}:{invoice.vendor_name}:{invoice.invoice_number}".encode("utf-8"))
                    res = ingest_invoice_record(
                        session=session,
                        invoice=invoice,
                        lines=lines,
                        file_sha256=row_hash,
                        source_filename=uploaded.name,
                        price_spike_threshold_pct=spike_pct,
                        near_duplicate_days=dup_days,
                        procurement_threshold=float(proc_threshold),
                        rule_weights=active_weights,
                    )
                    results.append(res)

        st.success(f"Processed {len(results)} invoice record(s).")
        st.json(results)
    except Exception as exc:
        st.error(f"Error: {exc}")

st.divider()
st.subheader("2. Spend & Risk Overview")

df_invoices = fetch_invoices_dataframe(engine)
if df_invoices.empty:
    st.info("No invoices stored yet. Upload a PDF, CSV, or XLSX invoice file above to begin.")
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Invoices", len(df_invoices))
    c2.metric("Total Spend", f"{df_invoices['grand_total_amount'].sum():,.2f}")
    c3.metric("High Risk Invoices", int((df_invoices["risk_level"] == "HIGH").sum()))
    c4.metric("Total Discrepancy", f"{df_invoices['discrepancy_amount'].abs().sum():,.2f}")

    vendor_df = fetch_vendor_spend_dataframe(engine)
    if not vendor_df.empty:
        col_v1, col_v2 = st.columns([2, 1])
        with col_v1:
            st.markdown("**Spend by Vendor**")
            st.bar_chart(vendor_df.set_index("vendor_name")["total_spend"])
        with col_v2:
            st.markdown("**Vendor Risk & Discrepancy Summary**")
            st.dataframe(vendor_df, use_container_width=True)

    risk_filter = st.multiselect("Filter by Risk Level", options=["LOW", "MEDIUM", "HIGH"], default=["LOW", "MEDIUM", "HIGH"])
    filtered_df = df_invoices[df_invoices["risk_level"].isin(risk_filter)] if risk_filter else df_invoices
    st.dataframe(filtered_df, use_container_width=True)

    selected_key = st.selectbox("Inspect Line Items & Audit Notes for Invoice Key", options=filtered_df["invoice_key"].tolist())
    if selected_key is not None:
        row_match = filtered_df[filtered_df["invoice_key"] == selected_key]
        if not row_match.empty:
            st.info(f"Audit Notes: {row_match.iloc[0]['summary_notes']}")
        st.markdown("**Line Items & Price Deviation**")
        st.dataframe(fetch_lines_for_invoice(engine, int(selected_key)), use_container_width=True)

    st.subheader("3. Clean Exports")
    col_e1, col_e2, col_e3 = st.columns(3)
    col_e1.download_button(
        label="Download CSV",
        data=filtered_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="invoices_intelligence.csv",
        mime="text/csv",
    )
    excel_buf = BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
        filtered_df.to_excel(writer, index=False, sheet_name="Invoices")
    col_e2.download_button(
        label="Download Excel (.xlsx)",
        data=excel_buf.getvalue(),
        file_name="invoices_intelligence.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    col_e3.download_button(
        label="Download JSON",
        data=filtered_df.to_json(orient="records", indent=2).encode("utf-8"),
        file_name="invoices_intelligence.json",
        mime="application/json",
    )
