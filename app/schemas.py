from datetime import date
from pydantic import BaseModel, Field


class ExtractedInvoice(BaseModel):
    vendor_name: str = Field(..., min_length=1)
    tax_identifier: str | None = None
    commercial_register: str | None = None
    bank_account: str | None = None
    beneficiary_name: str | None = None
    invoice_number: str = Field(..., min_length=1)
    issue_date: date
    currency: str = Field(default="DZD", min_length=2)
    subtotal_amount: float = 0.0
    tax_amount: float = 0.0
    discount_amount: float = 0.0
    grand_total_amount: float


class AuditedLineResult(BaseModel):
    line_number: int
    item_description: str
    normalized_item: str
    quantity: float
    unit_price: float
    line_total: float
    expected_line_total: float
    is_line_valid: bool
    historical_avg_price: float | None = None
    price_deviation_pct: float | None = None
    is_price_anomaly: bool = False


class IntelligenceVerdict(BaseModel):
    audit_status: str
    expected_total: float
    discrepancy_amount: float
    risk_score: int
    risk_level: str
    risk_flags: list[str]
    summary_notes: str
    lines: list[AuditedLineResult]
