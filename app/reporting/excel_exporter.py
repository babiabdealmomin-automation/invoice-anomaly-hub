"""
Executive Excel (.xlsx) Report Exporter for Invoice Spend & Anomaly Intelligence Hub.
Generates a multi-sheet audit workbook featuring:
1. `Summary` sheet: Executive KPIs (total invoices audited, counts by risk level,
   total spend, and amounts/variances under review).
2. `Invoices` sheet: Localized columns, frozen header row (Freeze Panes),
   AutoFilter, clear financial number formatting (#,##0.00), soft pastel risk-level
   highlighting, automatic column widths (preventing ###), and wrapped text for
   long audit notes.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Dict, Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def _normalize_risk_bucket(raw_risk: object) -> str:
    txt = str(raw_risk or "").strip().upper()
    if "HIGH" in txt or "مرتفع" in txt or "ÉLEVÉ" in txt or "ELEVE" in txt:
        return "HIGH"
    if "MEDIUM" in txt or "متوسط" in txt or "MOYEN" in txt:
        return "MEDIUM"
    return "LOW"


def build_executive_excel_report(
    df_raw: pd.DataFrame,
    df_display: pd.DataFrame,
    currency: str = "SAR",
    lang: str = "ar",
) -> bytes:
    """
    Build a formatted Excel workbook bytes payload with a Summary sheet first
    and a styled, auto-fitted, frozen-header Invoices detail sheet second.
    """
    wb = Workbook()
    is_rtl = lang == "ar"

    # Sheet names by language
    summary_title = "ملخص التدقيق (Summary)" if lang == "ar" else ("Synthèse (Summary)" if lang == "fr" else "Summary")
    details_title = "تفاصيل الفواتير (Invoices)" if lang == "ar" else ("Factures (Invoices)" if lang == "fr" else "Invoices")

    ws_summary = wb.active
    ws_summary.title = summary_title[:31]
    ws_details = wb.create_sheet(title=details_title[:31])

    ws_summary.views.sheetView[0].rightToLeft = is_rtl
    ws_details.views.sheetView[0].rightToLeft = is_rtl

    # Styling palette (calm, executive accounting colors)
    thin_border_side = Side(style="thin", color="D1D5DB")
    cell_border = Border(
        left=thin_border_side,
        right=thin_border_side,
        top=thin_border_side,
        bottom=thin_border_side,
    )
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=14, bold=True, color="0F172A")
    subtitle_font = Font(name="Calibri", size=10, italic=True, color="475569")
    kpi_label_font = Font(name="Calibri", size=11, bold=True, color="1E293B")
    kpi_val_font = Font(name="Calibri", size=11, bold=False, color="0F172A")

    # Calm pastel fills for risk rows/cells
    fill_high = PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid")     # Soft red
    fill_medium = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")   # Soft amber
    fill_low = PatternFill(start_color="ECFDF5", end_color="ECFDF5", fill_type="solid")      # Soft green
    fill_neutral = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    # -----------------------------------------------------------------------
    # 1. Populate Executive `Summary` Sheet
    # -----------------------------------------------------------------------
    total_invoices = len(df_raw)
    risk_buckets = (
        df_raw["risk_level"].apply(_normalize_risk_bucket)
        if "risk_level" in df_raw.columns
        else pd.Series(["LOW"] * total_invoices)
    )
    high_count = int((risk_buckets == "HIGH").sum())
    medium_count = int((risk_buckets == "MEDIUM").sum())
    low_count = int((risk_buckets == "LOW").sum())

    grand_totals = (
        pd.to_numeric(df_raw["grand_total_amount"], errors="coerce").fillna(0.0)
        if "grand_total_amount" in df_raw.columns
        else pd.Series([0.0] * total_invoices)
    )
    discrepancies = (
        pd.to_numeric(df_raw["discrepancy_amount"], errors="coerce").abs().fillna(0.0)
        if "discrepancy_amount" in df_raw.columns
        else pd.Series([0.0] * total_invoices)
    )

    total_spend = float(grand_totals.sum())
    under_review_mask = risk_buckets.isin(["HIGH", "MEDIUM"])
    amount_under_review = float(grand_totals[under_review_mask].sum())
    high_risk_amount = float(grand_totals[risk_buckets == "HIGH"].sum())
    total_variance = float(discrepancies.sum())

    if lang == "ar":
        report_heading = "التقرير التنفيذي لتدقيق الفواتير وكشف الشذوذ"
        generated_lbl = f"تاريخ التصدير: {datetime.now().strftime('%Y-%m-%d %H:%M')} | عملة التقييم: {currency}"
        col_metric = "المؤشر الرقابي والمالي"
        col_value = "القيمة"
        summary_rows = [
            ("إجمالي الفواتير المفحوصة", total_invoices, "#,##0", None),
            ("فواتير عالية الخطورة (HIGH)", high_count, "#,##0", fill_high if high_count > 0 else None),
            ("فواتير متوسطة الخطورة (MEDIUM)", medium_count, "#,##0", fill_medium if medium_count > 0 else None),
            ("فواتير سليمة / منخفضة الخطورة (LOW)", low_count, "#,##0", fill_low),
            (f"إجمالي قيمة الفواتير المفحوصة ({currency})", total_spend, f'#,##0.00 "{currency}"', None),
            (f"إجمالي المبالغ محل المراجعة (عالية + متوسطة) ({currency})", amount_under_review, f'#,##0.00 "{currency}"', fill_medium if amount_under_review > 0 else None),
            (f"إجمالي مبالغ الفواتير عالية الخطورة ({currency})", high_risk_amount, f'#,##0.00 "{currency}"', fill_high if high_risk_amount > 0 else None),
            (f"إجمالي الفروقات الحسابية المرصودة ({currency})", total_variance, f'#,##0.00 "{currency}"', fill_high if total_variance > 0 else None),
        ]
    elif lang == "fr":
        report_heading = "Rapport Exécutif d'Audit des Factures et Détection d'Anomalies"
        generated_lbl = f"Date d'export : {datetime.now().strftime('%Y-%m-%d %H:%M')} | Devise : {currency}"
        col_metric = "Indicateur d'Audit & Financier"
        col_value = "Valeur"
        summary_rows = [
            ("Total des factures auditées", total_invoices, "#,##0", None),
            ("Factures à risque élevé (HIGH)", high_count, "#,##0", fill_high if high_count > 0 else None),
            ("Factures à risque moyen (MEDIUM)", medium_count, "#,##0", fill_medium if medium_count > 0 else None),
            ("Factures conformes / risque faible (LOW)", low_count, "#,##0", fill_low),
            (f"Montant total audité ({currency})", total_spend, f'#,##0.00 "{currency}"', None),
            (f"Montant total sous révision (HIGH + MEDIUM) ({currency})", amount_under_review, f'#,##0.00 "{currency}"', fill_medium if amount_under_review > 0 else None),
            (f"Montant des factures à risque élevé ({currency})", high_risk_amount, f'#,##0.00 "{currency}"', fill_high if high_risk_amount > 0 else None),
            (f"Total des écarts arithmétiques détectés ({currency})", total_variance, f'#,##0.00 "{currency}"', fill_high if total_variance > 0 else None),
        ]
    else:
        report_heading = "Executive Invoice Audit & Anomaly Summary Report"
        generated_lbl = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Reporting Currency: {currency}"
        col_metric = "Audit & Financial Metric"
        col_value = "Value"
        summary_rows = [
            ("Total Invoices Audited", total_invoices, "#,##0", None),
            ("High-Risk Invoices (HIGH)", high_count, "#,##0", fill_high if high_count > 0 else None),
            ("Medium-Risk Invoices (MEDIUM)", medium_count, "#,##0", fill_medium if medium_count > 0 else None),
            ("Clean / Low-Risk Invoices (LOW)", low_count, "#,##0", fill_low),
            (f"Total Audited Spend ({currency})", total_spend, f'#,##0.00 "{currency}"', None),
            (f"Total Amount Under Review (HIGH + MEDIUM) ({currency})", amount_under_review, f'#,##0.00 "{currency}"', fill_medium if amount_under_review > 0 else None),
            (f"High-Risk Invoices Amount ({currency})", high_risk_amount, f'#,##0.00 "{currency}"', fill_high if high_risk_amount > 0 else None),
            (f"Total Math Discrepancy Detected ({currency})", total_variance, f'#,##0.00 "{currency}"', fill_high if total_variance > 0 else None),
        ]

    ws_summary["A1"] = report_heading
    ws_summary["A1"].font = title_font
    ws_summary["A2"] = generated_lbl
    ws_summary["A2"].font = subtitle_font

    ws_summary["A4"] = col_metric
    ws_summary["B4"] = col_value
    for col_idx in (1, 2):
        h_cell = ws_summary.cell(row=4, column=col_idx)
        h_cell.fill = header_fill
        h_cell.font = header_font
        h_cell.border = cell_border
        h_cell.alignment = Alignment(
            horizontal="right" if is_rtl else "left",
            vertical="center",
            wrap_text=True,
        )
    ws_summary.row_dimensions[4].height = 26

    for idx, (label, val, num_fmt, row_fill) in enumerate(summary_rows, start=5):
        c_lbl = ws_summary.cell(row=idx, column=1, value=label)
        c_val = ws_summary.cell(row=idx, column=2, value=val)

        c_lbl.font = kpi_label_font
        c_val.font = kpi_val_font
        c_lbl.border = cell_border
        c_val.border = cell_border
        c_val.number_format = num_fmt

        c_lbl.alignment = Alignment(horizontal="right" if is_rtl else "left", vertical="center")
        c_val.alignment = Alignment(horizontal="left" if is_rtl else "right", vertical="center")

        if row_fill is not None:
            c_lbl.fill = row_fill
            c_val.fill = row_fill
        ws_summary.row_dimensions[idx].height = 24

    ws_summary.column_dimensions["A"].width = 52
    ws_summary.column_dimensions["B"].width = 28
    ws_summary.freeze_panes = "A5"

    # -----------------------------------------------------------------------
    # 2. Populate Detailed `Invoices` Sheet
    # -----------------------------------------------------------------------
    if df_display.empty:
        out = BytesIO()
        wb.save(out)
        return out.getvalue()

    headers = list(df_display.columns)
    raw_cols = list(df_raw.columns)
    money_col_names = {
        "subtotal_amount",
        "tax_amount",
        "discount_amount",
        "grand_total_amount",
        "expected_grand_total",
        "discrepancy_amount",
    }
    score_col_names = {"risk_score", "extraction_confidence_score"}

    # Write header row
    ws_details.row_dimensions[1].height = 28
    for c_idx, col_title in enumerate(headers, start=1):
        cell = ws_details.cell(row=1, column=c_idx, value=str(col_title))
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )

    # Freeze top header row & enable AutoFilter across full table
    ws_details.freeze_panes = "A2"
    last_col_letter = get_column_letter(len(headers))
    ws_details.auto_filter.ref = f"A1:{last_col_letter}{len(df_display) + 1}"

    # Track max content widths for auto-sizing columns
    col_max_widths: Dict[int, int] = {
        i: max(len(str(h)) + 6, 14) for i, h in enumerate(headers, start=1)
    }

    for r_idx in range(len(df_display)):
        excel_row = r_idx + 2
        raw_row = df_raw.iloc[r_idx] if r_idx < len(df_raw) else pd.Series(dtype=object)
        row_currency = str(raw_row.get("currency") or currency).strip() or currency
        risk_bucket = _normalize_risk_bucket(raw_row.get("risk_level", "LOW"))

        if risk_bucket == "HIGH":
            row_fill = fill_high
        elif risk_bucket == "MEDIUM":
            row_fill = fill_medium
        else:
            row_fill = fill_low

        max_lines_in_row = 1

        for c_idx, disp_col in enumerate(headers, start=1):
            raw_col_key = raw_cols[c_idx - 1] if (c_idx - 1) < len(raw_cols) else disp_col
            val = df_display.iloc[r_idx, c_idx - 1]
            if pd.isna(val):
                val = ""

            cell = ws_details.cell(row=excel_row, column=c_idx)
            cell.border = cell_border
            cell.fill = row_fill

            if raw_col_key in money_col_names and val != "":
                try:
                    num_val = float(val)
                    cell.value = num_val
                    cell.number_format = f'#,##0.00 "{row_currency}"'
                    cell.alignment = Alignment(
                        horizontal="left" if is_rtl else "right",
                        vertical="center",
                    )
                    formatted_len = len(f"{num_val:,.2f} {row_currency}") + 4
                    col_max_widths[c_idx] = max(col_max_widths[c_idx], formatted_len)
                except (TypeError, ValueError):
                    cell.value = str(val)
            elif raw_col_key in score_col_names and val != "":
                try:
                    num_val = float(val)
                    cell.value = num_val
                    cell.number_format = "0.0"
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                except (TypeError, ValueError):
                    cell.value = str(val)
            else:
                txt_val = str(val)
                cell.value = txt_val
                is_long_text = raw_col_key in {"summary_notes", "reviewer_note", "vendor_name", "source_filename"}
                cell.alignment = Alignment(
                    horizontal="right" if is_rtl else "left",
                    vertical="center",
                    wrap_text=True,
                )
                if is_long_text:
                    # Estimate wrapped line count to avoid clipping long audit notes
                    target_w = 52 if raw_col_key == "summary_notes" else 30
                    col_max_widths[c_idx] = max(col_max_widths[c_idx], target_w)
                    explicit_lines = txt_val.count("\n") + 1
                    wrapped_lines = max(explicit_lines, (len(txt_val) // max(target_w - 4, 20)) + 1)
                    max_lines_in_row = max(max_lines_in_row, min(wrapped_lines, 8))
                else:
                    col_max_widths[c_idx] = max(col_max_widths[c_idx], min(len(txt_val) + 5, 36))

        ws_details.row_dimensions[excel_row].height = max(24, max_lines_in_row * 18)

    # Apply generous column widths so ### never appears
    for c_idx, width_val in col_max_widths.items():
        col_letter = get_column_letter(c_idx)
        ws_details.column_dimensions[col_letter].width = min(max(width_val, 15), 60)

    out_stream = BytesIO()
    wb.save(out_stream)
    return out_stream.getvalue()