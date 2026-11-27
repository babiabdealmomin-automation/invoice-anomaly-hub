"""
2-Way Purchase Order (PO) / Quotation Matching Engine.
Compares extracted invoice line items against an optional reference PO or
Quotation document (PDF, CSV, or XLSX) to detect:
- PO_PRICE_MISMATCH: Invoice unit_price exceeds agreed PO/Quotation unit_price.
- PO_QTY_EXCEEDED: Invoice quantity exceeds authorized PO/Quotation quantity.
- UNAPPROVED_ITEM: Invoice contains line items absent from the reference PO/Quotation.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from app.extraction.ai_parser import parse_invoice_pdf_native
from app.extraction.tabular_importer import parse_tabular_invoices


class ReferenceLineItem(BaseModel):
    """Normalized line item extracted from a reference PO or Quotation."""

    description: str
    normalized_key: str
    quantity: float = Field(default=0.0)
    unit_price: float = Field(default=0.0)
    line_total: float = Field(default=0.0)


class POMatchingFinding(BaseModel):
    """Individual discrepancy detected during 2-Way PO/Quotation matching."""

    rule_code: str  # PO_PRICE_MISMATCH | PO_QTY_EXCEEDED | UNAPPROVED_ITEM
    severity: str = Field(default="HIGH")
    item_description: str
    matched_po_description: Optional[str] = None
    invoice_value: str
    po_reference_value: str
    variance_detail: str


class POMatchingReport(BaseModel):
    """Structured outcome of 2-Way PO / Quotation verification."""

    po_reference_number: str = Field(default="N/A")
    po_vendor_name: str = Field(default="")
    total_po_items: int = Field(default=0)
    matched_items_count: int = Field(default=0)
    findings: List[POMatchingFinding] = Field(default_factory=list)
    has_discrepancies: bool = Field(default=False)


def _normalize_desc(text: Any) -> str:
    cleaned = re.sub(r"[^\w\s\u0600-\u06FF]", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        num = float(val)
        return default if num != num else num
    except (TypeError, ValueError):
        return default


def parse_reference_po_document(
    file_bytes: bytes,
    filename: str,
    api_key: str = "",
) -> Tuple[str, str, List[ReferenceLineItem]]:
    """
    Parse an optional reference Purchase Order or Quotation (PDF, CSV, or XLSX)
    into a normalized list of ReferenceLineItem objects.
    Returns (po_reference_number, po_vendor_name, reference_items).
    """
    lower_name = filename.lower().strip()
    ref_items: List[ReferenceLineItem] = []
    po_number = filename
    po_vendor = ""

    if lower_name.endswith(".pdf"):
        doc_header, doc_lines = parse_invoice_pdf_native(file_bytes, api_key=api_key)
        po_number = str(getattr(doc_header, "invoice_number", "") or filename).strip()
        po_vendor = str(getattr(doc_header, "vendor_name", "") or "").strip()
        for line in doc_lines:
            desc = str(getattr(line, "description", "") or "").strip()
            if not desc:
                continue
            qty = _safe_float(getattr(line, "quantity", 0.0))
            price = _safe_float(getattr(line, "unit_price", 0.0))
            total = _safe_float(getattr(line, "line_total", qty * price))
            ref_items.append(
                ReferenceLineItem(
                    description=desc,
                    normalized_key=_normalize_desc(desc),
                    quantity=qty,
                    unit_price=price,
                    line_total=total,
                )
            )
    else:
        batches = parse_tabular_invoices(file_bytes, filename)
        if batches:
            po_number = str(getattr(batches[0][0], "invoice_number", "") or filename).strip()
            po_vendor = str(getattr(batches[0][0], "vendor_name", "") or "").strip()
        for doc_header, doc_lines in batches:
            for line in doc_lines:
                desc = str(getattr(line, "description", "") or "").strip()
                if not desc:
                    continue
                qty = _safe_float(getattr(line, "quantity", 0.0))
                price = _safe_float(getattr(line, "unit_price", 0.0))
                total = _safe_float(getattr(line, "line_total", qty * price))
                ref_items.append(
                    ReferenceLineItem(
                        description=desc,
                        normalized_key=_normalize_desc(desc),
                        quantity=qty,
                        unit_price=price,
                        line_total=total,
                    )
                )

    return po_number, po_vendor, ref_items


def _find_best_po_match(
    inv_norm_desc: str,
    po_items: Sequence[ReferenceLineItem],
    similarity_threshold: float = 0.72,
) -> Optional[ReferenceLineItem]:
    """Match an invoice line description to the closest PO item via exact or fuzzy ratio."""
    if not inv_norm_desc or not po_items:
        return None

    # 1. Exact normalized match
    for item in po_items:
        if item.normalized_key == inv_norm_desc:
            return item

    # 2. Substring containment or token overlap / SequenceMatcher
    best_item: Optional[ReferenceLineItem] = None
    best_score = 0.0
    for item in po_items:
        if not item.normalized_key:
            continue
        if item.normalized_key in inv_norm_desc or inv_norm_desc in item.normalized_key:
            score = 0.90
        else:
            score = SequenceMatcher(None, inv_norm_desc, item.normalized_key).ratio()
        if score > best_score:
            best_score = score
            best_item = item

    return best_item if best_score >= similarity_threshold else None


def match_invoice_against_po(
    invoice: Any,
    invoice_lines: Sequence[Any],
    po_items: Sequence[ReferenceLineItem],
    po_reference_number: str = "PO-REF",
    po_vendor_name: str = "",
    price_tolerance_pct: float = 0.5,
    qty_tolerance: float = 0.001,
) -> POMatchingReport:
    """
    Execute 2-Way Matching between invoice lines and reference PO/Quotation lines.
    Detects:
      - UNAPPROVED_ITEM: Line item does not exist in the reference PO/Quotation.
      - PO_PRICE_MISMATCH: Invoice unit_price exceeds PO unit_price beyond tolerance.
      - PO_QTY_EXCEEDED: Invoice quantity exceeds PO quantity beyond tolerance.
    """
    currency = str(getattr(invoice, "currency", "") or "").strip()
    findings: List[POMatchingFinding] = []
    matched_count = 0

    # Track cumulative invoiced quantity per PO item in case a single PO item is billed across multiple lines
    accumulated_qty_by_po_key: Dict[str, float] = {}

    for line in invoice_lines:
        raw_desc = str(getattr(line, "description", "") or "").strip()
        if not raw_desc:
            continue
        norm_desc = _normalize_desc(raw_desc)
        inv_qty = _safe_float(getattr(line, "quantity", 0.0))
        inv_price = _safe_float(getattr(line, "unit_price", 0.0))

        matched_po = _find_best_po_match(norm_desc, po_items)
        if matched_po is None:
            findings.append(
                POMatchingFinding(
                    rule_code="UNAPPROVED_ITEM",
                    severity="HIGH",
                    item_description=raw_desc,
                    matched_po_description=None,
                    invoice_value=f"الكمية: {inv_qty:g} × السعر: {inv_price:,.2f} {currency}",
                    po_reference_value=f"غير مدرج في المستند المرجعي ({po_reference_number})",
                    variance_detail=f"بند غير معتمد في أمر الشراء/عرض السعر '{raw_desc}'",
                )
            )
            continue

        matched_count += 1

        # 1. Check Unit Price Mismatch (PO_PRICE_MISMATCH)
        if matched_po.unit_price > 0:
            allowed_max_price = matched_po.unit_price * (1.0 + price_tolerance_pct / 100.0)
            if inv_price > allowed_max_price:
                price_diff = inv_price - matched_po.unit_price
                pct_diff = (price_diff / matched_po.unit_price) * 100.0
                findings.append(
                    POMatchingFinding(
                        rule_code="PO_PRICE_MISMATCH",
                        severity="HIGH",
                        item_description=raw_desc,
                        matched_po_description=matched_po.description,
                        invoice_value=f"{inv_price:,.2f} {currency}",
                        po_reference_value=f"{matched_po.unit_price:,.2f} {currency}",
                        variance_detail=(
                            f"تجاوز سعر الوحدة المتفق عليه بمقدار +{price_diff:,.2f} {currency} (+{pct_diff:.1f}%)"
                        ),
                    )
                )

        # 2. Check Quantity Exceeded (PO_QTY_EXCEEDED)
        po_key = matched_po.normalized_key
        cumulative_qty = accumulated_qty_by_po_key.get(po_key, 0.0) + inv_qty
        accumulated_qty_by_po_key[po_key] = cumulative_qty

        if matched_po.quantity > 0 and cumulative_qty > (matched_po.quantity + qty_tolerance):
            qty_excess = cumulative_qty - matched_po.quantity
            findings.append(
                POMatchingFinding(
                    rule_code="PO_QTY_EXCEEDED",
                    severity="HIGH",
                    item_description=raw_desc,
                    matched_po_description=matched_po.description,
                    invoice_value=f"{cumulative_qty:g}",
                    po_reference_value=f"{matched_po.quantity:g}",
                    variance_detail=(
                        f"تجاوز الكمية المعتمدة في أمر الشراء بمقدار +{qty_excess:g} وحدة"
                    ),
                )
            )

    return POMatchingReport(
        po_reference_number=po_reference_number,
        po_vendor_name=po_vendor_name,
        total_po_items=len(po_items),
        matched_items_count=matched_count,
        findings=findings,
        has_discrepancies=len(findings) > 0,
    )