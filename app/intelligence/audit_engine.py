"""
Deterministic invoice auditing and risk-scoring engine.
Evaluates arithmetic accuracy, historical price spikes, near-duplicate & split
invoices, tax ID / bank account anomalies, future issue dates, and red-flag
indicators, returning structured alerts with:
- rule_triggered (اسم القاعدة المخالفة بالعربية)
- evidence_comparison (القيمة المسجلة في الفاتورة مقابل القيمة المرجعية/المتوقعة)
- recommended_action (الإجراء المحاسبي المطلوب قبل الصرف)
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from app.extraction.confidence_scorer import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    ExtractionConfidenceReport,
    evaluate_extraction_confidence,
)
from app.intelligence.po_matcher import (
    POMatchingReport,
    ReferenceLineItem,
    match_invoice_against_po,
)

DEFAULT_RULE_WEIGHTS: Dict[str, int] = {
    "ARITHMETIC_MISMATCH": 35,
    "PRICE_SPIKE": 25,
    "NEAR_DUPLICATE_INVOICE": 30,
    "SPLIT_INVOICE_PATTERN": 30,
    "HIGH_RISK_RED_FLAGS": 25,
    "THIRD_PARTY_OR_OFFSHORE_PAYEE": 35,
    "ROUND_HIGH_VALUE_TOTAL": 15,
    "NIF_MISMATCH": 30,
    "INVALID_NIF_FORMAT": 20,
    "BANK_ACCOUNT_CHANGED": 35,
    "NON_POSITIVE_GRAND_TOTAL": 40,
    "FUTURE_ISSUE_DATE": 35,
    "LOW_EXTRACTION_CONFIDENCE": 25,
    "PO_PRICE_MISMATCH": 35,
    "PO_QTY_EXCEEDED": 35,
    "UNAPPROVED_ITEM": 30,
    "VENDOR_VOLUME_SPIKE": 35,
}

RULE_ARABIC_NAMES: Dict[str, str] = {
    "ARITHMETIC_MISMATCH": "فرق في المطابقة الحسابية",
    "PRICE_SPIKE": "ارتفاع غير معتاد في سعر الوحدة",
    "NEAR_DUPLICATE_INVOICE": "اشتباه تكرار فاتورة لنفس المورّد",
    "SPLIT_INVOICE_PATTERN": "اشتباه تجزئة المشتريات لتجاوز سقف الاعتماد",
    "HIGH_RISK_RED_FLAGS": "مؤشرات رقابية تستوجب التحقق المستندي",
    "THIRD_PARTY_OR_OFFSHORE_PAYEE": "تحويل الدفع لطرف ثالث أو حساب خارجي",
    "ROUND_HIGH_VALUE_TOTAL": "مبلغ إجمالي مرتفع ومقرّب بالكامل",
    "NIF_MISMATCH": "اختلاف الرقم الضريبي عن سجل المورّد",
    "INVALID_NIF_FORMAT": "صيغة الرقم الضريبي غير مكتملة أو غير صحيحة",
    "BANK_ACCOUNT_CHANGED": "تغيير بيانات الحساب البنكي للمورّد",
    "NON_POSITIVE_GRAND_TOTAL": "فاتورة بإجمالي صفري أو سالب",
    "FUTURE_ISSUE_DATE": "تاريخ إصدار غير واقعي (مستقبلي)",
    "LOW_EXTRACTION_CONFIDENCE": "انخفاض ثقة القراءة الآلية (NEEDS_HUMAN_REVIEW)",
    "PO_PRICE_MISMATCH": "تجاوز سعر الوحدة لأمر الشراء / عرض السعر",
    "PO_QTY_EXCEEDED": "تجاوز الكمية المفوترة لأمر الشراء",
    "UNAPPROVED_ITEM": "بند غير مدرج في أمر الشراء / عرض السعر",
    "VENDOR_VOLUME_SPIKE": "قفزة غير معتادة في قيمة فاتورة المورّد التاريخية (VENDOR_VOLUME_SPIKE)",
}


class StructuredAuditAlert(BaseModel):
    """Structured audit finding emitted for each triggered rule."""

    rule_code: str = Field(..., description="Internal deterministic rule identifier")
    severity: str = Field(default="MEDIUM", description="LOW | MEDIUM | HIGH")
    weight_applied: int = Field(default=0, description="Risk points added by this rule")
    rule_triggered: str = Field(..., description="اسم القاعدة المخالفة بالعربية")
    evidence_comparison: str = Field(
        ..., description="القيمة المسجلة في الفاتورة مقابل القيمة المرجعية/المتوقعة"
    )
    recommended_action: str = Field(..., description="الإجراء المحاسبي المطلوب قبل الصرف")


class AuditResult(BaseModel):
    """Complete deterministic audit outcome for an invoice."""

    expected_grand_total: float
    discrepancy_amount: float
    risk_score: float
    risk_level: str
    extraction_confidence_score: float = 100.0
    needs_human_review: bool = False
    extraction_review_tag: str = "VERIFIED_EXTRACTION"
    extraction_report: Optional[ExtractionConfidenceReport] = None
    po_matching_report: Optional[POMatchingReport] = None
    alerts: List[StructuredAuditAlert] = Field(default_factory=list)
    summary_notes: str = ""


def _build_alert(
    rule_code: str,
    severity: str,
    weight_applied: int,
    evidence_comparison: str,
    recommended_action: str,
) -> StructuredAuditAlert:
    return StructuredAuditAlert(
        rule_code=rule_code,
        severity=severity,
        weight_applied=weight_applied,
        rule_triggered=RULE_ARABIC_NAMES.get(rule_code, rule_code),
        evidence_comparison=evidence_comparison,
        recommended_action=recommended_action,
    )


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_iso_date(val: Any) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    text = str(val).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def evaluate_invoice_audit(
    invoice: Any,
    lines: Sequence[Any],
    historical_avg_prices: Optional[Dict[str, float]] = None,
    recent_vendor_invoices: Optional[Sequence[Dict[str, Any]]] = None,
    known_vendor_nif: Optional[str] = None,
    known_vendor_bank: Optional[str] = None,
    price_spike_threshold_pct: float = 25.0,
    near_duplicate_days: int = 7,
    procurement_threshold: float = 100000.0,
    extraction_confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    reference_po_items: Optional[Sequence[ReferenceLineItem]] = None,
    po_reference_number: str = "PO-REF",
    po_vendor_name: str = "",
    vendor_historical_avg_invoice: Optional[float] = None,
    vendor_historical_invoice_count: int = 0,
    vendor_volume_spike_multiplier: float = 2.5,
    rule_weights: Optional[Dict[str, int]] = None,
) -> AuditResult:
    """
    Execute deterministic accounting & fraud rules on a single invoice and its lines.
    Every triggered rule produces a StructuredAuditAlert with:
    - rule_triggered
    - evidence_comparison
    - recommended_action
    """
    weights = {**DEFAULT_RULE_WEIGHTS, **(rule_weights or {})}
    hist_prices = {str(k).strip().lower(): float(v) for k, v in (historical_avg_prices or {}).items() if v}
    recent_invs = list(recent_vendor_invoices or [])

    # ------------------------------------------------------------------
    # 0. Post-Extraction Self-Verification & Confidence Scoring (0-100%)
    # ------------------------------------------------------------------
    confidence_report = evaluate_extraction_confidence(
        invoice=invoice,
        lines=lines,
        confidence_threshold=extraction_confidence_threshold,
    )

    currency = str(getattr(invoice, "currency", "") or "").strip()
    subtotal = _safe_float(getattr(invoice, "subtotal_amount", 0.0))
    tax_amount = _safe_float(getattr(invoice, "tax_amount", 0.0))
    discount_amount = _safe_float(getattr(invoice, "discount_amount", 0.0))
    grand_total = _safe_float(getattr(invoice, "grand_total_amount", 0.0))
    vendor_nif = str(getattr(invoice, "vendor_nif", "") or "").strip()
    vendor_bank = str(
        getattr(invoice, "vendor_bank_account", None)
        or getattr(invoice, "bank_account", None)
        or getattr(invoice, "iban", "")
        or ""
    ).strip()
    issue_dt = _parse_iso_date(
        getattr(invoice, "issue_date", None) or getattr(invoice, "invoice_date", None)
    )

    alerts: List[StructuredAuditAlert] = []
    total_score = 0.0

    w_conf = int(weights.get("LOW_EXTRACTION_CONFIDENCE", 0))
    if confidence_report.needs_human_review and w_conf > 0:
        total_score += w_conf
        flagged_items = confidence_report.missing_fields + confidence_report.suspect_fields
        flagged_str = "؛ ".join(flagged_items) if flagged_items else "عدم اكتمال أو تطابق حقول القراءة الآلية"
        alerts.append(
            _build_alert(
                rule_code="LOW_EXTRACTION_CONFIDENCE",
                severity="MEDIUM",
                weight_applied=w_conf,
                evidence_comparison=(
                    f"درجة ثقة الاستخراج: {confidence_report.confidence_score:.1f}% مقابل الحد الأدنى المعتمد "
                    f"{confidence_report.threshold_pct:g}% | الوسم: [{confidence_report.review_tag}] | "
                    f"الحقول المتأثرة: {flagged_str}"
                ),
                recommended_action="مراجعة المستند الأصلي (PDF/صورة) بشرياً واستكمال أو تصحيح الحقول الناقصة قبل اعتماد القيد.",
            )
        )

    # ------------------------------------------------------------------
    # 1. Arithmetic Reconciliation (Line items + Totals)
    # ------------------------------------------------------------------
    computed_lines_sum = 0.0
    line_math_errors: List[str] = []
    for idx, line in enumerate(lines, start=1):
        qty = _safe_float(getattr(line, "quantity", 0.0))
        unit_p = _safe_float(getattr(line, "unit_price", 0.0))
        line_tot = _safe_float(getattr(line, "line_total", 0.0))
        expected_line = round(qty * unit_p, 2)
        computed_lines_sum += expected_line
        if abs(line_tot - expected_line) > 0.05:
            line_math_errors.append(
                f"البند #{idx}: المسجّل {line_tot:,.2f} مقابل المتوقع {expected_line:,.2f} {currency}"
            )

    base_subtotal = computed_lines_sum if lines else subtotal
    expected_grand_total = round(base_subtotal + tax_amount - discount_amount, 2)
    discrepancy_amount = round(grand_total - expected_grand_total, 2)

    w_math = int(weights.get("ARITHMETIC_MISMATCH", 0))
    if w_math > 0 and (abs(discrepancy_amount) > 0.05 or line_math_errors):
        total_score += w_math
        evidence = (
            f"الإجمالي المسجّل في الفاتورة: {grand_total:,.2f} {currency} | "
            f"الإجمالي المحسوب المتوقع: {expected_grand_total:,.2f} {currency} | "
            f"مقدار الفارق: {discrepancy_amount:+,.2f} {currency}"
        )
        if line_math_errors:
            evidence += f" ({'؛ '.join(line_math_errors)})"
        alerts.append(
            _build_alert(
                rule_code="ARITHMETIC_MISMATCH",
                severity="HIGH",
                weight_applied=w_math,
                evidence_comparison=evidence,
                recommended_action="إيقاف الصرف ومطالبة المورّد بإصدار فاتورة مصححة أو إشعار دائن/مدين لتسوية الفارق الحسابي.",
            )
        )

    # ------------------------------------------------------------------
    # 2. Non-Positive Grand Total
    # ------------------------------------------------------------------
    w_nonpos = int(weights.get("NON_POSITIVE_GRAND_TOTAL", 0))
    if w_nonpos > 0 and grand_total <= 0.0:
        total_score += w_nonpos
        alerts.append(
            _build_alert(
                rule_code="NON_POSITIVE_GRAND_TOTAL",
                severity="HIGH",
                weight_applied=w_nonpos,
                evidence_comparison=(
                    f"المبلغ الإجمالي المسجّل: {grand_total:,.2f} {currency} | "
                    f"القيمة المرجعية المقبولة: أكبر من 0.00 {currency}"
                ),
                recommended_action="مراجعة المستند للتأكد مما إذا كان إشعار دائن (Credit Note) أو قيد تسوية قبل الترحيل المحاسبي.",
            )
        )

    # ------------------------------------------------------------------
    # 3. Future Issue Date Realism
    # ------------------------------------------------------------------
    w_future = int(weights.get("FUTURE_ISSUE_DATE", 0))
    today_dt = date.today()
    if w_future > 0 and issue_dt is not None and issue_dt > today_dt:
        days_ahead = (issue_dt - today_dt).days
        total_score += w_future
        alerts.append(
            _build_alert(
                rule_code="FUTURE_ISSUE_DATE",
                severity="HIGH",
                weight_applied=w_future,
                evidence_comparison=(
                    f"تاريخ الإصدار المسجّل: {issue_dt.isoformat()} | "
                    f"الحد المرجعي الأقصى (تاريخ اليوم): {today_dt.isoformat()} | "
                    f"التجاوز: +{days_ahead} يوم"
                ),
                recommended_action="رفض الفاتورة مؤقتاً وإعادتها للمورّد لتصحيح تاريخ الإصدار بما يطابق تاريخ التوريد الفعلي.",
            )
        )

    # ------------------------------------------------------------------
    # 4. Unit Price Spike vs. Vendor Historical Baseline
    # ------------------------------------------------------------------
    w_spike = int(weights.get("PRICE_SPIKE", 0))
    if w_spike > 0 and hist_prices:
        for line in lines:
            desc = str(getattr(line, "description", "") or "").strip()
            unit_p = _safe_float(getattr(line, "unit_price", 0.0))
            avg_p = hist_prices.get(desc.lower())
            if avg_p and avg_p > 0 and unit_p > avg_p:
                increase_pct = ((unit_p - avg_p) / avg_p) * 100.0
                if increase_pct > price_spike_threshold_pct:
                    total_score += w_spike
                    alerts.append(
                        _build_alert(
                            rule_code="PRICE_SPIKE",
                            severity="MEDIUM",
                            weight_applied=w_spike,
                            evidence_comparison=(
                                f"البند ({desc}): السعر المسجّل {unit_p:,.2f} {currency} مقابل "
                                f"متوسط السعر التاريخي للمورّد {avg_p:,.2f} {currency} "
                                f"(زيادة +{increase_pct:.1f}% تتجاوز العتبة {price_spike_threshold_pct:g}%)"
                            ),
                            recommended_action="مطابقة السعر مع العقد الإطاري أو أمر الشراء المعتمد وطلب مبرر خطي للزيادة السعرية قبل الاعتماد.",
                        )
                    )
                    break

    # ------------------------------------------------------------------
    # 5. Near-Duplicate Invoice Detection
    # ------------------------------------------------------------------
    w_dup = int(weights.get("NEAR_DUPLICATE_INVOICE", 0))
    if w_dup > 0 and recent_invs and grand_total > 0:
        for prev in recent_invs:
            prev_amt = _safe_float(prev.get("grand_total_amount", 0.0))
            prev_num = str(prev.get("invoice_number", "N/A"))
            prev_dt = _parse_iso_date(prev.get("invoice_date"))
            within_window = True
            days_diff_label = ""
            if issue_dt and prev_dt:
                diff_days = abs((issue_dt - prev_dt).days)
                within_window = diff_days <= near_duplicate_days
                days_diff_label = f"خلال {diff_days} يوم/أيام"
            if within_window and prev_amt > 0 and abs(grand_total - prev_amt) / prev_amt <= 0.01:
                total_score += w_dup
                alerts.append(
                    _build_alert(
                        rule_code="NEAR_DUPLICATE_INVOICE",
                        severity="HIGH",
                        weight_applied=w_dup,
                        evidence_comparison=(
                            f"مبلغ الفاتورة الحالية: {grand_total:,.2f} {currency} مقابل "
                            f"الفاتورة السابقة رقم ({prev_num}) بمبلغ {prev_amt:,.2f} {currency} {days_diff_label}"
                        ),
                        recommended_action="تعليق الصرف فوراً والتحقق من سجل المدفوعات ومحاضر الاستلام لمنع ازدواجية السداد لنفس التوريد.",
                    )
                )
                break

    # ------------------------------------------------------------------
    # 6. Split-Invoice Pattern (Below Direct Approval Limit)
    # ------------------------------------------------------------------
    w_split = int(weights.get("SPLIT_INVOICE_PATTERN", 0))
    if w_split > 0 and recent_invs and 0 < grand_total < procurement_threshold:
        window_sum = grand_total
        matched_nums: List[str] = []
        for prev in recent_invs:
            prev_amt = _safe_float(prev.get("grand_total_amount", 0.0))
            prev_dt = _parse_iso_date(prev.get("invoice_date"))
            if issue_dt and prev_dt and abs((issue_dt - prev_dt).days) > near_duplicate_days:
                continue
            if 0 < prev_amt < procurement_threshold:
                window_sum += prev_amt
                matched_nums.append(str(prev.get("invoice_number", "N/A")))
        if matched_nums and window_sum >= procurement_threshold:
            total_score += w_split
            alerts.append(
                _build_alert(
                    rule_code="SPLIT_INVOICE_PATTERN",
                    severity="HIGH",
                    weight_applied=w_split,
                    evidence_comparison=(
                        f"مجموع الفواتير المتقاربة للمورّد ({len(matched_nums) + 1} فواتير): "
                        f"{window_sum:,.2f} {currency} مقابل سقف الاعتماد المباشر {procurement_threshold:,.2f} {currency}"
                    ),
                    recommended_action="إحالة المعاملة للجنة المشتريات العليا والحصول على تفويض الصلاحية الأعلى للتحقق من عدم تجزئة الطلب.",
                )
            )

    # ------------------------------------------------------------------
    # 7. Tax ID (NIF) Format & Historical Consistency
    # ------------------------------------------------------------------
    w_nif_fmt = int(weights.get("INVALID_NIF_FORMAT", 0))
    if w_nif_fmt > 0 and vendor_nif:
        cleaned_nif = re.sub(r"\s+", "", vendor_nif)
        if not re.fullmatch(r"[A-Za-z0-9]{10,20}", cleaned_nif):
            total_score += w_nif_fmt
            alerts.append(
                _build_alert(
                    rule_code="INVALID_NIF_FORMAT",
                    severity="MEDIUM",
                    weight_applied=w_nif_fmt,
                    evidence_comparison=(
                        f"الرقم الضريبي المسجّل في الفاتورة: '{vendor_nif}' | "
                        f"الصيغة المرجعية المعتمدة: 10 إلى 20 خانة رقمية/حرفية نظامية"
                    ),
                    recommended_action="طلب صورة السجل التجاري والبطاقة الضريبية المحدّثة من المورّد قبل تسجيل الفاتورة ضريبياً.",
                )
            )

    w_nif_mis = int(weights.get("NIF_MISMATCH", 0))
    if w_nif_mis > 0 and vendor_nif and known_vendor_nif and vendor_nif != known_vendor_nif:
        total_score += w_nif_mis
        alerts.append(
            _build_alert(
                rule_code="NIF_MISMATCH",
                severity="HIGH",
                weight_applied=w_nif_mis,
                evidence_comparison=(
                    f"الرقم الضريبي في الفاتورة: '{vendor_nif}' مقابل "
                    f"الرقم الضريبي المعتمد في ملف المورّد: '{known_vendor_nif}'"
                ),
                recommended_action="تجميد الفاتورة والتحقق من الهوية القانونية للمورّد وتحديث بياناته الرسمية فقط بعد اعتماد الإدارة المالية.",
            )
        )

    # ------------------------------------------------------------------
    # 8. Vendor Bank Account / IBAN Change
    # ------------------------------------------------------------------
    w_bank = int(weights.get("BANK_ACCOUNT_CHANGED", 0))
    if w_bank > 0 and vendor_bank and known_vendor_bank and vendor_bank != known_vendor_bank:
        total_score += w_bank
        alerts.append(
            _build_alert(
                rule_code="BANK_ACCOUNT_CHANGED",
                severity="HIGH",
                weight_applied=w_bank,
                evidence_comparison=(
                    f"الحساب البنكي/الآيبان في الفاتورة: '{vendor_bank}' مقابل "
                    f"الحساب المعتمد مسبقاً للمورّد: '{known_vendor_bank}'"
                ),
                recommended_action="التحقق من رقم الآيبان عبر اتصال هاتفي موثق بالمورّد وطلب شهادة بنكية مختومة قبل الصرف.",
            )
        )

    # ------------------------------------------------------------------
    # 9. High-Risk Textual Red Flags & Third-Party / Offshore Payees
    # ------------------------------------------------------------------
    combined_text = " ".join(
        [
            str(getattr(invoice, "notes", "") or ""),
            str(getattr(invoice, "payment_terms", "") or ""),
            *(str(getattr(l, "description", "") or "") for l in lines),
        ]
    ).lower()

    w_offshore = int(weights.get("THIRD_PARTY_OR_OFFSHORE_PAYEE", 0))
    offshore_hits = [
        kw for kw in ("offshore", "third party", "third-party", "free zone", "طرف ثالث", "حساب خارجي")
        if kw in combined_text
    ]
    if w_offshore > 0 and offshore_hits:
        total_score += w_offshore
        alerts.append(
            _build_alert(
                rule_code="THIRD_PARTY_OR_OFFSHORE_PAYEE",
                severity="HIGH",
                weight_applied=w_offshore,
                evidence_comparison=(
                    f"مؤشرات مرصودة في بيانات السداد: ({', '.join(offshore_hits)}) مقابل "
                    f"السياسة المرجعية: السداد المباشر لحساب المورّد المتعاقد فقط"
                ),
                recommended_action="منع التحويل لطرف ثالث أو حساب خارجي وإحالة المستند لإدارة الامتثال والمراجعة الداخلية.",
            )
        )

    w_flags = int(weights.get("HIGH_RISK_RED_FLAGS", 0))
    red_flag_hits = [
        kw
        for kw in (
            "urgent",
            "no po",
            "without po",
            "misc services",
            "consulting fee",
            "بدون أمر شراء",
            "عاجل جداً",
            "خدمات متنوعة",
        )
        if kw in combined_text
    ]
    if w_flags > 0 and red_flag_hits:
        total_score += w_flags
        alerts.append(
            _build_alert(
                rule_code="HIGH_RISK_RED_FLAGS",
                severity="MEDIUM",
                weight_applied=w_flags,
                evidence_comparison=(
                    f"عبارات تستوجب التدقيق المستندي: ({', '.join(red_flag_hits)}) مقابل "
                    f"المتطلب الرقابي: وصف تفصيلي للخدمة/التوريد مرفق بأمر شراء معتمد"
                ),
                recommended_action="طلب إرفاق أمر الشراء (PO) ومحضر الاستلام الفني الموقّع وتفصيل نطاق الأعمال قبل اعتماد الصرف.",
            )
        )

    # ------------------------------------------------------------------
    # 10. Round High-Value Total
    # ------------------------------------------------------------------
    w_round = int(weights.get("ROUND_HIGH_VALUE_TOTAL", 0))
    if (
        w_round > 0
        and grand_total >= (procurement_threshold * 0.5)
        and grand_total % 1000 == 0
    ):
        total_score += w_round
        alerts.append(
            _build_alert(
                rule_code="ROUND_HIGH_VALUE_TOTAL",
                severity="LOW",
                weight_applied=w_round,
                evidence_comparison=(
                    f"المبلغ الإجمالي المسجّل: {grand_total:,.2f} {currency} (رقم صحيح مقرّب بالكامل) | "
                    f"حد الفحص المرجعي: {procurement_threshold * 0.5:,.2f} {currency}"
                ),
                recommended_action="مراجعة جدول الكميات والأسعار الإفرادية للتأكد من أن المبلغ ناتج عن تسعير فعلي وليس تقديراً جزافياً.",
            )
        )

    # ------------------------------------------------------------------
    # 11. 2-Way Purchase Order (PO) / Quotation Matching
    # ------------------------------------------------------------------
    po_report: Optional[POMatchingReport] = None
    if reference_po_items:
        po_report = match_invoice_against_po(
            invoice=invoice,
            invoice_lines=lines,
            po_items=reference_po_items,
            po_reference_number=po_reference_number,
            po_vendor_name=po_vendor_name,
        )
        po_actions = {
            "PO_PRICE_MISMATCH": "إيقاف الصرف ومطالبة المورّد بتعديل السعر ليتطابق مع أمر الشراء / عرض السعر المعتمد.",
            "PO_QTY_EXCEEDED": "حجز الكمية الزائدة ومراجعة محضر الاستلام المخزني وطلب أمر تغيير معتمد قبل الصرف.",
            "UNAPPROVED_ITEM": "رفض البند غير المعتمد وإعادته لإدارة المشتريات للتحقق من وجود تفويض مسبق لتوريده.",
        }
        for finding in po_report.findings:
            w_po = int(weights.get(finding.rule_code, 0))
            if w_po > 0:
                total_score += w_po
                alerts.append(
                    _build_alert(
                        rule_code=finding.rule_code,
                        severity=finding.severity,
                        weight_applied=w_po,
                        evidence_comparison=(
                            f"البند ({finding.item_description}) [{po_report.po_reference_number}]: "
                            f"المسجّل في الفاتورة ({finding.invoice_value}) مقابل المرجع ({finding.po_reference_value}) | "
                            f"{finding.variance_detail}"
                        ),
                        recommended_action=po_actions.get(
                            finding.rule_code,
                            "مطابقة البند مع أمر الشراء المعتمد قبل الصرف.",
                        ),
                    )
                )

    # ------------------------------------------------------------------
    # 12. Vendor Behavioral Profiling: VENDOR_VOLUME_SPIKE
    # ------------------------------------------------------------------
    w_vol = int(weights.get("VENDOR_VOLUME_SPIKE", 0))
    if (
        w_vol > 0
        and vendor_historical_invoice_count >= 3
        and vendor_historical_avg_invoice is not None
        and vendor_historical_avg_invoice > 0
        and grand_total >= (vendor_historical_avg_invoice * vendor_volume_spike_multiplier)
    ):
        multiple = grand_total / vendor_historical_avg_invoice
        total_score += w_vol
        alerts.append(
            _build_alert(
                rule_code="VENDOR_VOLUME_SPIKE",
                severity="HIGH",
                weight_applied=w_vol,
                evidence_comparison=(
                    f"قيمة الفاتورة الحالية: {grand_total:,.2f} {currency} مقابل المتوسط التاريخي للمورّد "
                    f"({vendor_historical_avg_invoice:,.2f} {currency} عبر {vendor_historical_invoice_count} فواتير سابقة) — "
                    f"تضاعف بمقدار {multiple:.1f}x (يتجاوز الحد {vendor_volume_spike_multiplier:g}x)"
                ),
                recommended_action="طلب موافقة استثنائية ومراجعة محاضر الاستلام والتعاقد للتحقق من سبب تضاعف قيمة الفاتورة عن نمط المورّد المعتاد.",
            )
        )

    final_score = min(100.0, round(total_score, 2))
    if final_score >= 60.0 or any(a.severity == "HIGH" for a in alerts):
        risk_level = "HIGH"
    elif final_score >= 25.0 or alerts:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    if alerts:
        summary_notes = " || ".join(
            f"[{a.rule_triggered}] {a.evidence_comparison} -> الإجراء المطلوب: {a.recommended_action}"
            for a in alerts
        )
    else:
        summary_notes = "اجتازت الفاتورة جميع فحوصات المطابقة الحسابية والرقابية بنجاح."

    return AuditResult(
        expected_grand_total=expected_grand_total,
        discrepancy_amount=discrepancy_amount,
        risk_score=final_score,
        risk_level=risk_level,
        extraction_confidence_score=confidence_report.confidence_score,
        needs_human_review=confidence_report.needs_human_review,
        extraction_review_tag=confidence_report.review_tag,
        extraction_report=confidence_report,
        po_matching_report=po_report,
        alerts=alerts,
        summary_notes=summary_notes,
    )