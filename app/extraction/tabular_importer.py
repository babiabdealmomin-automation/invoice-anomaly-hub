from datetime import datetime
from io import BytesIO
import pandas as pd
from app.schemas import ExtractedInvoice


def _parse_date(val) -> datetime.date:
    if hasattr(val, "date"):
        return val.date()
    text = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported invoice date format: {val}")


def parse_tabular_invoices(file_bytes: bytes, filename: str) -> list[tuple[ExtractedInvoice, list[dict]]]:
    lower_name = filename.lower()
    if lower_name.endswith(".csv"):
        df = pd.read_csv(BytesIO(file_bytes))
    elif lower_name.endswith(".xlsx") or lower_name.endswith(".xls"):
        df = pd.read_excel(BytesIO(file_bytes))
    else:
        raise ValueError("Only CSV, XLSX, and XLS files are supported by the tabular importer.")

    required_cols = {
        "vendor_name",
        "invoice_number",
        "issue_date",
        "item_description",
        "quantity",
        "unit_price",
        "line_total",
        "grand_total_amount",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns in tabular file: {sorted(missing)}")

    df = df.copy()
    has_subtotal_col = "subtotal_amount" in df.columns
    df["currency"] = df["currency"] if "currency" in df.columns else "USD"
    df["tax_identifier"] = df["tax_identifier"] if "tax_identifier" in df.columns else None
    df["subtotal_amount"] = df["subtotal_amount"] if has_subtotal_col else df["line_total"]
    df["tax_amount"] = df["tax_amount"] if "tax_amount" in df.columns else 0.0
    df["discount_amount"] = df["discount_amount"] if "discount_amount" in df.columns else 0.0

    grouped_results: list[tuple[ExtractedInvoice, list[dict]]] = []

    for (vendor_name, inv_num), grp in df.groupby(["vendor_name", "invoice_number"], sort=False):
        first = grp.iloc[0]
        issue_dt = _parse_date(first["issue_date"])
        subtotal_val = float(first["subtotal_amount"]) if has_subtotal_col else float(grp["line_total"].sum())
        tax_val = float(first["tax_amount"]) if pd.notna(first["tax_amount"]) else 0.0
        discount_val = float(first["discount_amount"]) if pd.notna(first["discount_amount"]) else 0.0
        grand_val = float(first["grand_total_amount"])

        invoice = ExtractedInvoice(
            vendor_name=str(vendor_name).strip(),
            tax_identifier=str(first["tax_identifier"]).strip() if pd.notna(first["tax_identifier"]) and str(first["tax_identifier"]).strip() else None,
            invoice_number=str(inv_num).strip(),
            issue_date=issue_dt,
            currency=str(first["currency"]).strip() or "USD",
            subtotal_amount=subtotal_val,
            tax_amount=tax_val,
            discount_amount=discount_val,
            grand_total_amount=grand_val,
        )

        lines: list[dict] = []
        for idx, (_, row) in enumerate(grp.iterrows(), start=1):
            lines.append(
                {
                    "line_number": idx,
                    "item_description": str(row["item_description"]).strip(),
                    "quantity": float(row["quantity"]),
                    "unit_price": float(row["unit_price"]),
                    "line_total": float(row["line_total"]),
                }
            )

        grouped_results.append((invoice, lines))

    return grouped_results
