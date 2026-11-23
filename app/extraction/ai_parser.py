from io import BytesIO
import json
from google import genai
from google.genai import types
from app.config import settings
from app.schemas import ExtractedInvoice


EXTRACTION_PROMPT = """
You are a financial document parser for an invoice spend and anomaly intelligence hub.
Extract structured invoice data directly from the attached PDF document.
Rules:
1. Never invent missing numbers; if subtotal, tax, or discount is absent, set to 0.0.
2. When multiple tax rates or VAT tiers appear (e.g., 19%, 9%, 0%, TVA), sum all tax amounts into tax_amount while keeping grand_total_amount equal to the final invoice total shown.
3. Sum all discounts, rebates, or reductions into discount_amount as a positive number.
4. Convert dates to YYYY-MM-DD (including Arabic month names like "يوليو" -> 07 or "نوفمبر" -> 11).
5. Normalize currency symbols like "$" to "USD", "€" to "EUR", or "د.ج" / "DA" to "DZD" when clear.
6. vendor_name is the issuing supplier/company/entity, not the buyer/recipient.
7. Extract tax_identifier (NIF), commercial_register (RC), bank_account (RIB/IBAN), and beneficiary_name if present on the invoice; otherwise null.
8. Each item in lines must have line_number (1-based integer), item_description, quantity, unit_price, and line_total.
"""

INVOICE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "vendor_name": {"type": "STRING"},
        "tax_identifier": {"type": "STRING", "nullable": True},
        "commercial_register": {"type": "STRING", "nullable": True},
        "bank_account": {"type": "STRING", "nullable": True},
        "beneficiary_name": {"type": "STRING", "nullable": True},
        "invoice_number": {"type": "STRING"},
        "issue_date": {"type": "STRING", "description": "YYYY-MM-DD"},
        "currency": {"type": "STRING"},
        "subtotal_amount": {"type": "NUMBER"},
        "tax_amount": {"type": "NUMBER"},
        "discount_amount": {"type": "NUMBER"},
        "grand_total_amount": {"type": "NUMBER"},
        "lines": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "line_number": {"type": "INTEGER"},
                    "item_description": {"type": "STRING"},
                    "quantity": {"type": "NUMBER"},
                    "unit_price": {"type": "NUMBER"},
                    "line_total": {"type": "NUMBER"},
                },
                "required": ["line_number", "item_description", "quantity", "unit_price", "line_total"],
            },
        },
    },
    "required": [
        "vendor_name",
        "invoice_number",
        "issue_date",
        "currency",
        "subtotal_amount",
        "tax_amount",
        "discount_amount",
        "grand_total_amount",
        "lines",
    ],
}


def parse_invoice_pdf_native(file_bytes: bytes, api_key: str | None = None) -> tuple[ExtractedInvoice, list[dict]]:
    if not file_bytes:
        raise ValueError("Uploaded PDF file is empty.")

    resolved_key = (api_key or settings.GEMINI_API_KEY).strip()
    if not resolved_key:
        raise RuntimeError("GEMINI_API_KEY is missing. Set it in .env or sidebar.")

    client = genai.Client(api_key=resolved_key)
    uploaded_doc = client.files.upload(
        file=BytesIO(file_bytes),
        config={"mime_type": "application/pdf"},
    )
    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[uploaded_doc, EXTRACTION_PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=INVOICE_SCHEMA,
                temperature=0.0,
            ),
        )
    finally:
        try:
            client.files.delete(name=uploaded_doc.name)
        except Exception:
            pass

    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")

    data = json.loads(response.text)
    raw_lines = data.pop("lines", [])
    invoice = ExtractedInvoice.model_validate(data)
    return invoice, raw_lines
