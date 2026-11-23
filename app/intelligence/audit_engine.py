import re
from app.schemas import AuditedLineResult, ExtractedInvoice, IntelligenceVerdict

TOLERANCE = 0.01

DEFAULT_RULE_WEIGHTS: dict[str, int] = {
    "ARITHMETIC_MISMATCH": 45,
    "PRICE_SPIKE": 30,
    "NEAR_DUPLICATE_INVOICE": 35,
    "SPLIT_INVOICE_PATTERN": 30,
    "HIGH_RISK_RED_FLAGS": 30,
    "THIRD_PARTY_OR_OFFSHORE_PAYEE": 25,
    "NIF_MISMATCH": 25,
    "BANK_ACCOUNT_CHANGED": 25,
    "INVALID_NIF_FORMAT": 15,
    "ROUND_HIGH_VALUE_TOTAL": 15,
    "NON_POSITIVE_GRAND_TOTAL": 40,
}

HIGH_RISK_KEYWORDS = (
    "offshore",
    "طرف ثالث",
    "third party",
    "بدون سند",
    "بدون وصل",
    "بدون أمر",
    "شفهي",
    "غير متعاقد",
    "وساطة",
    "تسهيلات",
    "جزافي",
    "استشارات",
    "عاجل",
    "فوري",
)

OFFSHORE_PAYEE_KEYWORDS = ("offshore", "منطقة حرة", "طرف ثالث", "third party", "AE")


def normalize_item_label(description: str) -> str:
    cleaned = re.sub(r"[^\w\s\u0600-\u06FF]", " ", description.lower().strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def is_valid_algerian_nif(nif: str | None) -> bool:
    if not nif:
        return False
    digits = re.sub(r"\D", "", nif)
    return len(digits) in (15, 20)


def score_to_risk_level(score: int) -> str:
    if score >= 60:
        return "HIGH"
    if score >= 25:
        return "MEDIUM"
    return "LOW"


def _has_high_risk_red_flags(invoice: ExtractedInvoice, lines: list[dict]) -> list[str]:
    found: list[str] = []
    combined_text = " ".join(
        [
            str(invoice.vendor_name or ""),
            str(invoice.invoice_number or ""),
            str(invoice.bank_account or ""),
            str(invoice.beneficiary_name or ""),
            *[str(ln.get("item_description", "")) for ln in lines],
        ]
    ).lower()

    matched_kw = [kw for kw in HIGH_RISK_KEYWORDS if kw in combined_text]
    if len(matched_kw) >= 2:
        found.append("HIGH_RISK_RED_FLAGS")

    if float(invoice.grand_total_amount) >= 1_000_000 and float(invoice.grand_total_amount) % 500_000 == 0:
        found.append("ROUND_HIGH_VALUE_TOTAL")

    beneficiary_blob = f"{invoice.beneficiary_name or ''} {invoice.bank_account or ''}".lower()
    if any(k.lower() in beneficiary_blob for k in OFFSHORE_PAYEE_KEYWORDS):
        found.append("THIRD_PARTY_OR_OFFSHORE_PAYEE")

    return found


def _detect_split_invoice(
    grand_total: float,
    split_candidates: list[float] | None,
    procurement_threshold: float,
) -> bool:
    if not split_candidates or grand_total <= 0:
        return False
    combined_sum = grand_total + sum(float(x) for x in split_candidates)
    under_or_near_threshold = grand_total <= procurement_threshold * 1.05
    return under_or_near_threshold and (combined_sum >= procurement_threshold)


def evaluate_invoice_intelligence(
    invoice: ExtractedInvoice,
    lines: list[dict],
    historical_item_prices: dict[str, float] | None = None,
    has_near_duplicate: bool = False,
    price_spike_threshold_pct: float = 25.0,
    known_vendor_nif: str | None = None,
    known_vendor_bank: str | None = None,
    split_invoice_candidates: list[float] | None = None,
    procurement_threshold: float = 3_500_000.0,
    rule_weights: dict[str, int] | None = None,
) -> IntelligenceVerdict:
    history = historical_item_prices or {}
    weights = {**DEFAULT_RULE_WEIGHTS, **(rule_weights or {})}

    def _w(rule_name: str) -> int:
        return max(0, int(weights.get(rule_name, 0)))

    risk_score = 0
    flags: list[str] = []
    notes: list[str] = []
    audited_lines: list[AuditedLineResult] = []

    line_sum = 0.0
    any_line_invalid = False
    spike_rule_enabled = _w("PRICE_SPIKE") > 0

    for raw in lines:
        qty = float(raw["quantity"])
        price = float(raw["unit_price"])
        extracted_total = float(raw["line_total"])
        expected_line = round(qty * price, 4)
        line_sum += expected_line

        is_valid = (qty > 0) and (price >= 0) and (abs(extracted_total - expected_line) <= TOLERANCE)
        if not is_valid:
            any_line_invalid = True

        norm_item = normalize_item_label(str(raw["item_description"]))
        hist_avg = history.get(norm_item)
        dev_pct: float | None = None
        is_spike = False

        if spike_rule_enabled and hist_avg is not None and hist_avg > 0:
            dev_pct = round(((price - hist_avg) / hist_avg) * 100.0, 2)
            if dev_pct > price_spike_threshold_pct:
                is_spike = True
                if "PRICE_SPIKE" not in flags:
                    flags.append("PRICE_SPIKE")
                notes.append(
                    f"Line #{raw['line_number']} ({raw['item_description']}) unit price {price} is +{dev_pct}% above historical avg {hist_avg:.2f}."
                )

        audited_lines.append(
            AuditedLineResult(
                line_number=int(raw["line_number"]),
                item_description=str(raw["item_description"]),
                normalized_item=norm_item,
                quantity=qty,
                unit_price=price,
                line_total=extracted_total,
                expected_line_total=expected_line,
                is_line_valid=is_valid,
                historical_avg_price=hist_avg,
                price_deviation_pct=dev_pct,
                is_price_anomaly=is_spike,
            )
        )

    expected_total = round(line_sum + float(invoice.tax_amount) - float(invoice.discount_amount), 4)
    discrepancy = round(float(invoice.grand_total_amount) - expected_total, 4)

    subtotal_ok = abs(float(invoice.subtotal_amount) - round(line_sum, 4)) <= TOLERANCE
    grand_ok = abs(discrepancy) <= TOLERANCE

    if invoice.grand_total_amount <= 0 and _w("NON_POSITIVE_GRAND_TOTAL") > 0:
        risk_score += _w("NON_POSITIVE_GRAND_TOTAL")
        flags.append("NON_POSITIVE_GRAND_TOTAL")
        notes.append("Grand total must be positive.")

    if (any_line_invalid or not subtotal_ok or not grand_ok) and _w("ARITHMETIC_MISMATCH") > 0:
        risk_score += _w("ARITHMETIC_MISMATCH")
        flags.append("ARITHMETIC_MISMATCH")
        notes.append(
            f"Arithmetic discrepancy detected: grand_total={invoice.grand_total_amount}, expected_total={expected_total}, diff={discrepancy}."
        )

    if (
        _w("INVALID_NIF_FORMAT") > 0
        and invoice.tax_identifier
        and not invoice.tax_identifier.upper().startswith("NIF-")
        and not is_valid_algerian_nif(invoice.tax_identifier)
    ):
        risk_score += _w("INVALID_NIF_FORMAT")
        flags.append("INVALID_NIF_FORMAT")
        notes.append(f"Tax identifier '{invoice.tax_identifier}' does not match standard 15/20-digit NIF format.")

    if (
        _w("NIF_MISMATCH") > 0
        and invoice.tax_identifier
        and known_vendor_nif
        and re.sub(r"\D", "", invoice.tax_identifier) != re.sub(r"\D", "", known_vendor_nif)
    ):
        risk_score += _w("NIF_MISMATCH")
        flags.append("NIF_MISMATCH")
        notes.append(f"NIF changed from '{known_vendor_nif}' to '{invoice.tax_identifier}'.")

    clean_bank = re.sub(r"\s+", "", (invoice.bank_account or "")).upper()
    clean_known_bank = re.sub(r"\s+", "", (known_vendor_bank or "")).upper()
    if _w("BANK_ACCOUNT_CHANGED") > 0 and clean_bank and clean_known_bank and clean_bank != clean_known_bank:
        risk_score += _w("BANK_ACCOUNT_CHANGED")
        flags.append("BANK_ACCOUNT_CHANGED")
        notes.append(f"Bank account changed from '{clean_known_bank}' to '{clean_bank}'.")

    if "PRICE_SPIKE" in flags and spike_rule_enabled:
        risk_score += _w("PRICE_SPIKE")

    if has_near_duplicate and _w("NEAR_DUPLICATE_INVOICE") > 0:
        risk_score += _w("NEAR_DUPLICATE_INVOICE")
        flags.append("NEAR_DUPLICATE_INVOICE")
        notes.append("Similar invoice amount and date found for the same vendor within recent window.")

    if (
        _w("SPLIT_INVOICE_PATTERN") > 0
        and _detect_split_invoice(float(invoice.grand_total_amount), split_invoice_candidates, procurement_threshold)
    ):
        risk_score += _w("SPLIT_INVOICE_PATTERN")
        flags.append("SPLIT_INVOICE_PATTERN")
        combined = float(invoice.grand_total_amount) + sum(float(x) for x in (split_invoice_candidates or []))
        notes.append(
            f"Potential split-invoice pattern: combined spend {combined:,.2f} across recent invoices reaches/exceeds threshold {procurement_threshold:,.2f}."
        )

    red_flags = _has_high_risk_red_flags(invoice, lines)
    if "HIGH_RISK_RED_FLAGS" in red_flags and _w("HIGH_RISK_RED_FLAGS") > 0:
        risk_score += _w("HIGH_RISK_RED_FLAGS")
        flags.append("HIGH_RISK_RED_FLAGS")
        notes.append("High-risk procurement/payment red flags detected in invoice terms or descriptions.")

    if "THIRD_PARTY_OR_OFFSHORE_PAYEE" in red_flags and _w("THIRD_PARTY_OR_OFFSHORE_PAYEE") > 0:
        risk_score += _w("THIRD_PARTY_OR_OFFSHORE_PAYEE")
        flags.append("THIRD_PARTY_OR_OFFSHORE_PAYEE")
        notes.append("Third-party or offshore payment beneficiary/account detected.")

    if "ROUND_HIGH_VALUE_TOTAL" in red_flags and _w("ROUND_HIGH_VALUE_TOTAL") > 0:
        risk_score += _w("ROUND_HIGH_VALUE_TOTAL")
        flags.append("ROUND_HIGH_VALUE_TOTAL")
        notes.append("Large round grand total detected.")

    risk_score = min(100, risk_score)
    risk_level = score_to_risk_level(risk_score)
    audit_status = "PASSED" if (not any_line_invalid and subtotal_ok and grand_ok) else "FAILED"

    if not notes:
        notes.append("Invoice passed deterministic arithmetic, identifier, and anomaly checks.")

    return IntelligenceVerdict(
        audit_status=audit_status,
        expected_total=expected_total,
        discrepancy_amount=discrepancy,
        risk_score=risk_score,
        risk_level=risk_level,
        risk_flags=flags,
        summary_notes=" ".join(notes),
        lines=audited_lines,
    )
