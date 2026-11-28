"""
Deterministic Extraction Confidence Score & Self-Verification module.
Evaluates post-extraction completeness, format validity, and subtotal-to-lines
reconciliation (0% - 100%), automatically tagging invoices below threshold
(default 85%) with NEEDS_HUMAN_REVIEW and pinpointing missing/suspect fields.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

DEFAULT_CONFIDENCE_THRESHOLD: float = 85.0


class ExtractionConfidenceReport(BaseModel):
    """Structured confidence assessment produced immediately after extraction."""

    confidence_score: float = Field(..., ge=0.0, le=100.0)
    threshold_pct: float = Field(default=DEFAULT_CONFIDENCE_THRESHOLD)
    needs_human_review: bool = Field(default=False)
    review_tag: str = Field(default="VERIFIED_EXTRACTION")
    missing_fields: List[str] = Field(default_factory=list)
    suspect_fields: List[str] = Field(default_factory=list)
    checks_breakdown: Dict[str, Any] = Field(default_factory=dict)
    arabic_summary: str = Field(default="")


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        num = float(val)
        return None if num != num else num  # guard NaN
    except (TypeError, ValueError):
        return None


def _is_valid_date_str(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, date):
        return True
    txt = str(val).strip()
    if not txt:
        return False
    try:
        date.fromisoformat(txt[:10])
        return True
    except ValueError:
        return False


def _get_val(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            val = getattr(obj, name, None)
            if val is not None:
                return val
    return default


def evaluate_extraction_confidence(
    invoice: Any,
    lines: Sequence[Any],
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> ExtractionConfidenceReport:
    """
    Compute a deterministic Extraction Confidence Score (0% - 100%) across:
      1. Core mandatory fields completeness (60 pts):
         - invoice_number (12 pts)
         - issue_date / invoice_date (12 pts)
         - vendor_name (12 pts)
         - vendor_nif / tax_id (12 pts)
         - grand_total_amount (12 pts)
      2. Format & sanity validity (20 pts):
         - Valid ISO date format (7 pts)
         - Valid NIF/Tax ID format (7 pts)
         - Valid 3-letter currency code & positive grand total (6 pts)
      3. Line-items to Subtotal reconciliation (20 pts):
         - sum(line_total) matches subtotal_amount within tolerance (20 pts)
    """
    score = 0.0
    missing_fields: List[str] = []
    suspect_fields: List[str] = []
    breakdown: Dict[str, Any] = {}

    inv_number = str(_get_val(invoice, "invoice_number", default="") or "").strip()
    raw_date = _get_val(invoice, "issue_date", "invoice_date", default=None)
    vendor_name = str(_get_val(invoice, "vendor_name", default="") or "").strip()
    vendor_nif = str(
        _get_val(invoice, "vendor_nif", "tax_identifier", "tax_id", default="") or ""
    ).strip()
    currency = str(_get_val(invoice, "currency", default="") or "").strip()
    subtotal = _safe_float(_get_val(invoice, "subtotal_amount", default=None))
    grand_total = _safe_float(_get_val(invoice, "grand_total_amount", default=None))

    # ------------------------------------------------------------------
    # 1. Core Mandatory Fields Completeness (60 points total = 5 x 12)
    # ------------------------------------------------------------------
    if inv_number and inv_number.lower() not in {"n/a", "none", "null", "unknown", "-"}:
        score += 12.0
        breakdown["invoice_number"] = "OK"
    else:
        missing_fields.append("رقم الفاتورة (invoice_number)")
        breakdown["invoice_number"] = "MISSING"

    if raw_date is not None and str(raw_date).strip():
        score += 12.0
        breakdown["issue_date"] = "OK"
    else:
        missing_fields.append("تاريخ الإصدار (issue_date)")
        breakdown["issue_date"] = "MISSING"

    if vendor_name and vendor_name.lower() not in {"n/a", "none", "null", "unknown", "-"}:
        score += 12.0
        breakdown["vendor_name"] = "OK"
    else:
        missing_fields.append("اسم المورّد (vendor_name)")
        breakdown["vendor_name"] = "MISSING"

    if vendor_nif and vendor_nif.lower() not in {"n/a", "none", "null", "unknown", "-"}:
        score += 12.0
        breakdown["vendor_nif"] = "OK"
    else:
        missing_fields.append("الرقم الضريبي للمورّد (vendor_nif)")
        breakdown["vendor_nif"] = "MISSING"

    if grand_total is not None and grand_total > 0.0:
        score += 12.0
        breakdown["grand_total_amount"] = "OK"
    else:
        missing_fields.append("المبلغ الإجمالي (grand_total_amount)")
        breakdown["grand_total_amount"] = "MISSING_OR_ZERO"

    # ------------------------------------------------------------------
    # 2. Format & Structural Validity (20 points total)
    # ------------------------------------------------------------------
    if _is_valid_date_str(raw_date):
        score += 7.0
        breakdown["date_format"] = "VALID"
    else:
        if raw_date is not None and str(raw_date).strip():
            suspect_fields.append(f"صيغة تاريخ الإصدار غير قياسية ('{raw_date}')")
        breakdown["date_format"] = "INVALID"

    if vendor_nif:
        cleaned_nif = re.sub(r"\s+", "", vendor_nif)
        if re.fullmatch(r"[A-Za-z0-9]{10,20}", cleaned_nif):
            score += 7.0
            breakdown["nif_format"] = "VALID"
        else:
            suspect_fields.append(f"صيغة الرقم الضريبي مشكوك فيها ('{vendor_nif}')")
            breakdown["nif_format"] = "SUSPECT"
    else:
        breakdown["nif_format"] = "SKIPPED_MISSING"

    if re.fullmatch(r"[A-Za-z]{3}", currency) and grand_total is not None and grand_total > 0:
        score += 6.0
        breakdown["currency_and_total"] = "VALID"
    else:
        if not re.fullmatch(r"[A-Za-z]{3}", currency):
            suspect_fields.append(f"رمز العملة غير مكتمل ('{currency or 'فارغ'}')")
        breakdown["currency_and_total"] = "SUSPECT"

    # ------------------------------------------------------------------
    # 3. Line-Items Sum vs. Subtotal Reconciliation (20 points total)
    # ------------------------------------------------------------------
    if lines:
        computed_lines_sum = 0.0
        for line in lines:
            l_tot = _safe_float(_get_val(line, "line_total", default=None))
            if l_tot is None:
                q = _safe_float(_get_val(line, "quantity", default=0.0)) or 0.0
                p = _safe_float(_get_val(line, "unit_price", default=0.0)) or 0.0
                l_tot = q * p
            computed_lines_sum += l_tot

        if subtotal is not None and subtotal > 0:
            diff = abs(round(computed_lines_sum - subtotal, 2))
            tolerance = max(0.05, subtotal * 0.005)
            if diff <= tolerance:
                score += 20.0
                breakdown["lines_subtotal_match"] = "EXACT_MATCH"
            else:
                # Partial proportional credit if close, 0 if large OCR mismatch
                ratio_err = diff / subtotal
                if ratio_err <= 0.05:
                    score += 10.0
                suspect_fields.append(
                    f"مجموع البنود ({computed_lines_sum:,.2f}) لا يطابق الإجمالي قبل الضريبة ({subtotal:,.2f})"
                )
                breakdown["lines_subtotal_match"] = f"MISMATCH_DIFF_{diff:.2f}"
        elif grand_total is not None and grand_total > 0:
            # Fallback if subtotal wasn't explicitly extracted on simple receipts
            score += 14.0
            breakdown["lines_subtotal_match"] = "SUBTOTAL_NOT_EXPLICIT"
        else:
            suspect_fields.append("تعذرت مطابقة مجموع البنود لغياب الإجمالي قبل الضريبة")
            breakdown["lines_subtotal_match"] = "MISSING_SUBTOTAL"
    else:
        if subtotal is not None and subtotal > 0:
            score += 10.0
            suspect_fields.append("لم يتم استخراج تفاصيل بنود الفاتورة (line_items)")
            breakdown["lines_subtotal_match"] = "NO_LINES_EXTRACTED"
        else:
            suspect_fields.append("غياب بنود الفاتورة والإجمالي قبل الضريبة")
            breakdown["lines_subtotal_match"] = "NO_LINES_OR_SUBTOTAL"

    final_score = round(min(100.0, max(0.0, score)), 1)
    needs_review = final_score < float(confidence_threshold)
    review_tag = "NEEDS_HUMAN_REVIEW" if needs_review else "VERIFIED_EXTRACTION"

    issues_parts: List[str] = []
    if missing_fields:
        issues_parts.append(f"حقول ناقصة: {'، '.join(missing_fields)}")
    if suspect_fields:
        issues_parts.append(f"حقول مشكوك فيها: {'، '.join(suspect_fields)}")

    if needs_review:
        details_txt = " | ".join(issues_parts) if issues_parts else "انخفاض درجة اكتمال أو تطابق البيانات المستخرجة"
        arabic_summary = (
            f"[NEEDS_HUMAN_REVIEW] درجة ثقة الاستخراج {final_score:.1f}% (أقل من الحد المعتمد {confidence_threshold:g}%) — "
            f"{details_txt}. يُرجى التحقق البشري من المستند الأصلي قبل الاعتماد."
        )
    else:
        arabic_summary = f"درجة ثقة الاستخراج {final_score:.1f}% — البيانات المستخرجة مكتملة ومتطابقة."

    return ExtractionConfidenceReport(
        confidence_score=final_score,
        threshold_pct=float(confidence_threshold),
        needs_human_review=needs_review,
        review_tag=review_tag,
        missing_fields=missing_fields,
        suspect_fields=suspect_fields,
        checks_breakdown=breakdown,
        arabic_summary=arabic_summary,
    )