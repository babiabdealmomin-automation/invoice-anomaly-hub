"""
Streamlit interactive dashboard for Invoice Spend & Anomaly Intelligence Hub.
Self-contained multilingual UI (Arabic with RTL, English, French) and
Streamlit Community Cloud sys.path compatibility.
"""

import re
import sys
import uuid
from io import BytesIO
from pathlib import Path
from typing import Dict, Set

# Ensure project root is on sys.path (critical for Streamlit Community Cloud)
ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
(ROOT_DIR / "data").mkdir(parents=True, exist_ok=True)

import pandas as pd
import streamlit as st

from app.config import settings
from app.extraction.ai_parser import parse_invoice_pdf_native
from app.extraction.pdf_reader import compute_sha256
from app.extraction.tabular_importer import parse_tabular_invoices
from app.intelligence.audit_engine import DEFAULT_RULE_WEIGHTS
from app.warehouse.models import FactInvoice, build_engine, build_session_factory
from app.warehouse.repository import (
    fetch_invoices_dataframe,
    fetch_lines_for_invoice,
    fetch_vendor_spend_dataframe,
    init_schema,
    ingest_invoice_record,
    reset_warehouse,
)

# ---------------------------------------------------------------------------
# Embedded Internationalization (i18n) Dictionary (AR / EN / FR)
# ---------------------------------------------------------------------------
SUPPORTED_LANGUAGES: Dict[str, str] = {
    "ar": "العربية (AR)",
    "en": "English (EN)",
    "fr": "Français (FR)",
}

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "ar": {
        "app_title": "منصة ذكاء مصروفات الفواتير وكشف الشذوذ",
        "app_subtitle": "استخراج ذكي لملفات PDF • تدقيق حسابي حتمي • تصدير تقارير المستودع",
        "sidebar_settings": "⚙️ إعدادات التدقيق",
        "sidebar_workspace_session": "🏢 مساحة العمل والجلسة المعزولة",
        "workspace_mode": "وضع العزل",
        "mode_isolated_session": "جلسة مستقلة تماماً (معزولة لكل مستخدم)",
        "mode_named_workspace": "مساحة عمل مخصصة (حسب اسم القسم/المشروع)",
        "workspace_name_input": "اسم مساحة العمل",
        "active_workspace_badge": "مساحة العمل النشطة: {ws}",
        "new_isolated_session_btn": "🆕 بدء جلسة جديدة معزولة",
        "gemini_api_key": "مفتاح Gemini API",
        "price_spike_pct": "عتبة قفزة السعر (%)",
        "near_duplicate_days": "نافذة الفواتير شبه المكررة والتجزئة (أيام)",
        "procurement_threshold": "سقف الصفقات / المشتريات المرجعي (دج)",
        "sidebar_warehouse": "🗄️ صيانة مستودع البيانات",
        "reset_warehouse_btn": "🗑️ إعادة تهيئة المستودع",
        "reset_warehouse_success": "تم تفريغ المستودع وإعادة تهيئته بنجاح.",
        "sidebar_rule_weights": "⚖️ أوزان وتفعيل قواعد المخاطر",
        "rule_weight": "وزن القاعدة: {label}",
        "section_ingest": "1. استيراد الفواتير (PDF / CSV / XLSX)",
        "upload_label": "ارفع ملفات الفواتير (PDF أو CSV أو XLSX)",
        "upload_help": "يمكنك تحديد ورفع عدة ملفات دفعة واحدة باللغات العربية والفرنسية والإنجليزية مع منع التكرار الحرفي عبر بصمة SHA-256.",
        "process_btn": "🚀 تحليل وتخزين الملفات في المستودع",
        "processing_spinner": "جارٍ استخراج البيانات وإجراء التدقيق الحسابي وتقييم المخاطر...",
        "processing_file": "جارٍ معالجة الملف ({current}/{total}): {filename}...",
        "upload_success": "تمت معالجة وتخزين {count} سجل فاتورة من {files} ملف(ات) بنجاح.",
        "duplicate_in_batch": "تم تخطي الملف المكرر داخل الدفعة المرفوعة: '{filename}' (مطابق تماماً لملف '{original}').",
        "duplicate_in_warehouse": "تم تخطي الملف '{filename}': موجود مسبقاً في مستودع البيانات (بصمة SHA-256 متطابقة).",
        "duplicate_summary": "⚠️ تم تخطي {skipped} ملف(ات) مكررة تلقائياً لحماية المستودع وتوفير استهلاك API.",
        "file_error": "تعذرت معالجة الملف '{filename}': {err}",
        "error_prefix": "خطأ",
        "section_overview": "2. نظرة عامة على المصروفات والمخاطر",
        "no_data_yet": "لا توجد فواتير مخزنة بعد. ارفع ملف PDF أو CSV أو XLSX أعلاه للبدء.",
        "kpi_total_invoices": "عدد الفواتير",
        "kpi_total_spend": "إجمالي المصروفات",
        "kpi_high_risk": "فواتير عالية الخطورة",
        "kpi_total_discrepancy": "إجمالي الفروقات الحسابية",
        "spend_by_vendor": "المصروفات حسب المورّد",
        "vendor_risk_summary": "ملخص المخاطر والفروقات حسب المورّد",
        "filter_risk_level": "تصفية حسب مستوى الخطورة",
        "inspect_invoice_key": "فحص البنود وملاحظات التدقيق لرمز الفاتورة",
        "audit_notes_prefix": "ملاحظات التدقيق",
        "line_items_deviation": "تفاصيل البنود وانحراف الأسعار",
        "section_exports": "3. تصدير التقارير النظيفة",
        "export_csv": "⬇️ تحميل CSV",
        "export_excel": "⬇️ تحميل Excel (.xlsx)",
        "export_json": "⬇️ تحميل JSON",
        "risk_LOW": "منخفض (LOW)",
        "risk_MEDIUM": "متوسط (MEDIUM)",
        "risk_HIGH": "مرتفع (HIGH)",
        # Audit Rules (Labels)
        "rule_ARITHMETIC_MISMATCH": "عدم تطابق حسابي",
        "rule_PRICE_SPIKE": "قفزة سعرية غير مبررة",
        "rule_NEAR_DUPLICATE_INVOICE": "فاتورة شبه مكررة",
        "rule_SPLIT_INVOICE_PATTERN": "نمط تجزئة الفواتير",
        "rule_HIGH_RISK_RED_FLAGS": "مؤشرات احتيال عالية الخطورة",
        "rule_THIRD_PARTY_OR_OFFSHORE_PAYEE": "دفع لطرف ثالث أو حساب خارجي (Offshore)",
        "rule_ROUND_HIGH_VALUE_TOTAL": "مبلغ إجمالي كبير ومستدير",
        "rule_NIF_MISMATCH": "تعارض المعرف الجبائي (NIF)",
        "rule_INVALID_NIF_FORMAT": "صيغة معرف جبائي (NIF) غير صالحة",
        "rule_BANK_ACCOUNT_CHANGED": "تغيّر الحساب البنكي للمورّد",
        "rule_NON_POSITIVE_GRAND_TOTAL": "مبلغ إجمالي صفري أو سالب",
        # Audit Rules (Descriptions)
        "desc_ARITHMETIC_MISMATCH": "وجود فرق حسابي بين (الكمية × سعر الوحدة) وإجمالي البند أو بين المجموع الفرعي والضرائب والمبلغ الإجمالي.",
        "desc_PRICE_SPIKE": "ارتفاع سعر الوحدة عن المتوسط التاريخي لنفس المورّد بنسبة تتجاوز العتبة المحددة.",
        "desc_NEAR_DUPLICATE_INVOICE": "رصد فاتورة أخرى لنفس المورّد بمبلغ متقارب جداً ضمن النافذة الزمنية المحددة.",
        "desc_SPLIT_INVOICE_PATTERN": "احتمال تقسيم مشتريات إلى عدة فواتير متقاربة للبقاء تحت سقف الصفقات المعتمد.",
        "desc_HIGH_RISK_RED_FLAGS": "وجود عبارات عالية الخطورة مثل غياب سند الطلب أو تعيينات مبهمة أو استعجال غير مبرر.",
        "desc_THIRD_PARTY_OR_OFFSHORE_PAYEE": "توجيه الدفع إلى مستفيد طرف ثالث أو حسابات/جهات خارجية (Offshore) أو مناطق حرة.",
        "desc_ROUND_HIGH_VALUE_TOTAL": "المبلغ الإجمالي مرتفع ومستدير تماماً مما يستدعي التحقق من التسعير الفعلي.",
        "desc_NIF_MISMATCH": "اختلاف المعرف الجبائي (NIF) المسجل في الفاتورة عن المعرف التاريخي المعتمد للمورّد.",
        "desc_INVALID_NIF_FORMAT": "صيغة المعرف الجبائي (NIF) لا تطابق البنية النظامية المطلوبة.",
        "desc_BANK_ACCOUNT_CHANGED": "تغيّر رقم الحساب البنكي (RIB/IBAN) للمورّد مقارنة بالفواتير السابقة.",
        "desc_NON_POSITIVE_GRAND_TOTAL": "المبلغ الإجمالي للفاتورة يساوي صفراً أو يحمل قيمة سالبة.",
    },
    "en": {
        "app_title": "Invoice Spend & Anomaly Intelligence Hub",
        "app_subtitle": "Native PDF Document Intelligence • Deterministic Audit • Warehouse Exports",
        "sidebar_settings": "⚙️ Configuration",
        "sidebar_workspace_session": "🏢 Workspace & Session Isolation",
        "workspace_mode": "Isolation Mode",
        "mode_isolated_session": "Isolated User Session (Private per browser)",
        "mode_named_workspace": "Named Workspace (Department / Project)",
        "workspace_name_input": "Workspace Name",
        "active_workspace_badge": "Active Workspace: {ws}",
        "new_isolated_session_btn": "🆕 Start New Isolated Session",
        "gemini_api_key": "Gemini API Key",
        "price_spike_pct": "Price Spike Threshold (%)",
        "near_duplicate_days": "Near-Duplicate & Split Window (Days)",
        "procurement_threshold": "Procurement Threshold (DZD)",
        "sidebar_warehouse": "🗄️ Warehouse Maintenance",
        "reset_warehouse_btn": "🗑️ Reset Warehouse",
        "reset_warehouse_success": "Warehouse cleared and reinitialized.",
        "sidebar_rule_weights": "⚖️ Risk Rule Weights & Toggles",
        "rule_weight": "{label} Weight",
        "section_ingest": "1. Ingest Invoices (PDF / CSV / XLSX)",
        "upload_label": "Upload invoice PDF, CSV, or XLSX files",
        "upload_help": "Select and upload multiple Arabic, French, or English invoice files at once with SHA-256 exact-duplicate protection.",
        "process_btn": "🚀 Analyze & Store Files in Warehouse",
        "processing_spinner": "Extracting document data, running deterministic math audit, and scoring risks...",
        "processing_file": "Processing file ({current}/{total}): {filename}...",
        "upload_success": "Processed {count} invoice record(s) across {files} file(s).",
        "duplicate_in_batch": "Skipped duplicate file in uploaded batch: '{filename}' (identical to '{original}').",
        "duplicate_in_warehouse": "Skipped file '{filename}': already exists in the warehouse (matching SHA-256 hash).",
        "duplicate_summary": "⚠️ Automatically skipped {skipped} duplicate file(s) to protect the warehouse and save API usage.",
        "file_error": "Failed to process file '{filename}': {err}",
        "error_prefix": "Error",
        "section_overview": "2. Spend & Risk Overview",
        "no_data_yet": "No invoices stored yet. Upload a PDF, CSV, or XLSX invoice file above to begin.",
        "kpi_total_invoices": "Total Invoices",
        "kpi_total_spend": "Total Spend",
        "kpi_high_risk": "High Risk Invoices",
        "kpi_total_discrepancy": "Total Discrepancy",
        "spend_by_vendor": "Spend by Vendor",
        "vendor_risk_summary": "Vendor Risk & Discrepancy Summary",
        "filter_risk_level": "Filter by Risk Level",
        "inspect_invoice_key": "Inspect Line Items & Audit Notes for Invoice Key",
        "audit_notes_prefix": "Audit Notes",
        "line_items_deviation": "Line Items & Price Deviation",
        "section_exports": "3. Clean Exports",
        "export_csv": "⬇️ Download CSV",
        "export_excel": "⬇️ Download Excel (.xlsx)",
        "export_json": "⬇️ Download JSON",
        "risk_LOW": "LOW",
        "risk_MEDIUM": "MEDIUM",
        "risk_HIGH": "HIGH",
        "rule_ARITHMETIC_MISMATCH": "Arithmetic Mismatch",
        "rule_PRICE_SPIKE": "Price Spike",
        "rule_NEAR_DUPLICATE_INVOICE": "Near-Duplicate Invoice",
        "rule_SPLIT_INVOICE_PATTERN": "Split-Invoice Pattern",
        "rule_HIGH_RISK_RED_FLAGS": "High-Risk Red Flags",
        "rule_THIRD_PARTY_OR_OFFSHORE_PAYEE": "Third-Party / Offshore Payee",
        "rule_ROUND_HIGH_VALUE_TOTAL": "Large Round Total",
        "rule_NIF_MISMATCH": "NIF Mismatch",
        "rule_INVALID_NIF_FORMAT": "Invalid NIF Format",
        "rule_BANK_ACCOUNT_CHANGED": "Bank Account Changed",
        "rule_NON_POSITIVE_GRAND_TOTAL": "Non-Positive Total",
        "desc_ARITHMETIC_MISMATCH": "Mathematical discrepancy between (quantity × unit price) and line total, or subtotal/tax/discount vs. grand total.",
        "desc_PRICE_SPIKE": "Unit price exceeds the vendor's historical average by more than the configured percentage threshold.",
        "desc_NEAR_DUPLICATE_INVOICE": "Another invoice from the same vendor with a closely matching amount was found within the rolling time window.",
        "desc_SPLIT_INVOICE_PATTERN": "Multiple invoices from the same vendor may have been split to remain below the procurement ceiling.",
        "desc_HIGH_RISK_RED_FLAGS": "High-risk textual indicators detected (e.g., missing purchase order, vague descriptions, or unjustified urgency).",
        "desc_THIRD_PARTY_OR_OFFSHORE_PAYEE": "Payment directed to a third-party beneficiary, offshore entity/account, or free-zone jurisdiction.",
        "desc_ROUND_HIGH_VALUE_TOTAL": "Unusually large, perfectly rounded invoice total warranting pricing verification.",
        "desc_NIF_MISMATCH": "Invoice Tax ID (NIF) differs from the vendor's previously recorded Tax ID.",
        "desc_INVALID_NIF_FORMAT": "Vendor Tax ID (NIF) does not conform to the expected regulatory format.",
        "desc_BANK_ACCOUNT_CHANGED": "Vendor bank account details (RIB/IBAN) changed compared to historical records.",
        "desc_NON_POSITIVE_GRAND_TOTAL": "Invoice grand total is zero or negative.",
    },
    "fr": {
        "app_title": "Plateforme d'Audit des Factures et Détection d'Anomalies",
        "app_subtitle": "Extraction native PDF • Audit arithmétique déterministe • Exports d'entrepôt",
        "sidebar_settings": "⚙️ Configuration",
        "sidebar_workspace_session": "🏢 Espace de Travail & Session Isolée",
        "workspace_mode": "Mode d'isolation",
        "mode_isolated_session": "Session isolée (Privée par utilisateur)",
        "mode_named_workspace": "Espace de travail nommé (Département / Projet)",
        "workspace_name_input": "Nom de l'espace de travail",
        "active_workspace_badge": "Espace actif : {ws}",
        "new_isolated_session_btn": "🆕 Nouvelle session isolée",
        "gemini_api_key": "Clé API Gemini",
        "price_spike_pct": "Seuil de hausse de prix (%)",
        "near_duplicate_days": "Fenêtre doublons / fractionnement (jours)",
        "procurement_threshold": "Seuil des marchés publics (DZD)",
        "sidebar_warehouse": "🗄️ Maintenance de l'Entrepôt",
        "reset_warehouse_btn": "🗑️ Réinitialiser l'Entrepôt",
        "reset_warehouse_success": "L'entrepôt a été vidé et réinitialisé avec succès.",
        "sidebar_rule_weights": "⚖️ Pondération et Activation des Règles",
        "rule_weight": "Poids : {label}",
        "section_ingest": "1. Importer des factures (PDF / CSV / XLSX)",
        "upload_label": "Téléverser des factures PDF, CSV ou XLSX",
        "upload_help": "Sélectionnez et téléversez plusieurs fichiers (arabe, français, anglais) simultanément avec protection anti-doublon SHA-256.",
        "process_btn": "🚀 Analyser et Stocker les Fichiers",
        "processing_spinner": "Extraction des données, audit arithmétique et calcul du score de risque...",
        "processing_file": "Traitement du fichier ({current}/{total}) : {filename}...",
        "upload_success": "{count} facture(s) traitée(s) et enregistrée(s) depuis {files} fichier(s).",
        "duplicate_in_batch": "Fichier en double ignoré dans le lot : « {filename} » (identique à « {original} »).",
        "duplicate_in_warehouse": "Fichier « {filename} » ignoré : déjà présent dans l'entrepôt (empreinte SHA-256 identique).",
        "duplicate_summary": "⚠️ {skipped} fichier(s) en double ignoré(s) automatiquement pour protéger l'entrepôt.",
        "file_error": "Échec du traitement du fichier « {filename} » : {err}",
        "error_prefix": "Erreur",
        "section_overview": "2. Aperçu des Dépenses et des Risques",
        "no_data_yet": "Aucune facture enregistrée. Téléversez un fichier PDF, CSV ou XLSX ci-dessus pour commencer.",
        "kpi_total_invoices": "Nombre de Factures",
        "kpi_total_spend": "Dépenses Totales",
        "kpi_high_risk": "Factures à Haut Risque",
        "kpi_total_discrepancy": "Écart Arithmétique Total",
        "spend_by_vendor": "Dépenses par Fournisseur",
        "vendor_risk_summary": "Synthèse des Risques et Écarts par Fournisseur",
        "filter_risk_level": "Filtrer par niveau de risque",
        "inspect_invoice_key": "Inspecter les lignes et notes d'audit pour la clé facture",
        "audit_notes_prefix": "Notes d'audit",
        "line_items_deviation": "Lignes de facture et écart de prix",
        "section_exports": "3. Exports Propres",
        "export_csv": "⬇️ Télécharger CSV",
        "export_excel": "⬇️ Télécharger Excel (.xlsx)",
        "export_json": "⬇️ Télécharger JSON",
        "risk_LOW": "FAIBLE (LOW)",
        "risk_MEDIUM": "MOYEN (MEDIUM)",
        "risk_HIGH": "ÉLEVÉ (HIGH)",
        "rule_ARITHMETIC_MISMATCH": "Écart arithmétique",
        "rule_PRICE_SPIKE": "Hausse anormale de prix",
        "rule_NEAR_DUPLICATE_INVOICE": "Facture quasi-dupliquée",
        "rule_SPLIT_INVOICE_PATTERN": "Fractionnement de factures",
        "rule_HIGH_RISK_RED_FLAGS": "Signaux d'alerte à haut risque",
        "rule_THIRD_PARTY_OR_OFFSHORE_PAYEE": "Bénéficiaire tiers / compte offshore",
        "rule_ROUND_HIGH_VALUE_TOTAL": "Montant élevé arrondi",
        "rule_NIF_MISMATCH": "Incohérence du NIF",
        "rule_INVALID_NIF_FORMAT": "Format de NIF invalide",
        "rule_BANK_ACCOUNT_CHANGED": "Changement de RIB/IBAN fournisseur",
        "rule_NON_POSITIVE_GRAND_TOTAL": "Montant total nul ou négatif",
        "desc_ARITHMETIC_MISMATCH": "Écart de calcul entre (quantité × prix unitaire) et le total ligne, ou entre HT/TVA/remise et TTC.",
        "desc_PRICE_SPIKE": "Le prix unitaire dépasse la moyenne historique du fournisseur au-delà du seuil configuré.",
        "desc_NEAR_DUPLICATE_INVOICE": "Une autre facture du même fournisseur avec un montant très proche a été détectée dans la fenêtre temporelle.",
        "desc_SPLIT_INVOICE_PATTERN": "Possibilité de fractionnement des achats en plusieurs factures pour rester sous le seuil des marchés.",
        "desc_HIGH_RISK_RED_FLAGS": "Indicateurs textuels à haut risque détectés (absence de bon de commande, libellés vagues, urgence injustifiée).",
        "desc_THIRD_PARTY_OR_OFFSHORE_PAYEE": "Paiement orienté vers un tiers bénéficiaire, une entité offshore ou une zone franche.",
        "desc_ROUND_HIGH_VALUE_TOTAL": "Montant total élevé et parfaitement arrondi nécessitant une vérification tarifaire.",
        "desc_NIF_MISMATCH": "Le NIF de la facture diffère du NIF historique enregistré pour ce fournisseur.",
        "desc_INVALID_NIF_FORMAT": "Le format du NIF ne respecte pas la structure réglementaire attendue.",
        "desc_BANK_ACCOUNT_CHANGED": "Les coordonnées bancaires (RIB/IBAN) du fournisseur ont changé par rapport à l'historique.",
        "desc_NON_POSITIVE_GRAND_TOTAL": "Le montant total TTC de la facture est nul ou négatif.",
    },
}

COLUMN_LABELS: Dict[str, Dict[str, str]] = {
    "ar": {
        "invoice_key": "رمز الفاتورة",
        "invoice_number": "رقم الفاتورة",
        "vendor_name": "اسم المورّد",
        "invoice_date": "تاريخ الفاتورة",
        "currency": "العملة",
        "subtotal_amount": "المجموع قبل الضريبة",
        "tax_amount": "الضريبة",
        "discount_amount": "الخصم",
        "grand_total_amount": "المبلغ الإجمالي",
        "discrepancy_amount": "الفرق الحسابي",
        "risk_score": "نقاط المخاطر",
        "risk_level": "مستوى الخطورة",
        "summary_notes": "ملاحظات التدقيق",
        "total_spend": "إجمالي المصروفات",
        "invoice_count": "عدد الفواتير",
        "avg_risk_score": "متوسط المخاطر",
        "total_discrepancy": "إجمالي الفروقات",
        "description": "البيان / الوصف",
        "quantity": "الكمية",
        "unit_price": "سعر الوحدة",
        "line_total": "إجمالي البند",
        "expected_line_total": "الإجمالي المتوقع",
        "price_deviation_pct": "انحراف السعر (%)",
    },
    "fr": {
        "invoice_key": "Clé Facture",
        "invoice_number": "N° Facture",
        "vendor_name": "Fournisseur",
        "invoice_date": "Date Facture",
        "currency": "Devise",
        "subtotal_amount": "Montant HT",
        "tax_amount": "TVA",
        "discount_amount": "Remise",
        "grand_total_amount": "Montant TTC",
        "discrepancy_amount": "Écart",
        "risk_score": "Score Risque",
        "risk_level": "Niveau Risque",
        "summary_notes": "Notes d'Audit",
        "total_spend": "Dépenses Totales",
        "invoice_count": "Nb Factures",
        "avg_risk_score": "Risque Moyen",
        "total_discrepancy": "Écart Total",
        "description": "Désignation",
        "quantity": "Quantité",
        "unit_price": "Prix Unitaire",
        "line_total": "Total Ligne",
        "expected_line_total": "Total Attendu",
        "price_deviation_pct": "Écart Prix (%)",
    },
}


def t(key: str, lang: str = "ar", **kwargs) -> str:
    """Return translated UI text with safe fallback to English."""
    lang_dict = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    text = lang_dict.get(key) or TRANSLATIONS["en"].get(key) or key
    return text.format(**kwargs) if kwargs else text


def get_rule_label(rule_code: str, lang: str = "ar") -> str:
    """Translate an audit rule code into a readable label."""
    return t(f"rule_{rule_code}", lang=lang)


def get_rule_description(rule_code: str, lang: str = "ar") -> str:
    """Return an explanation tooltip for an audit rule."""
    return t(f"desc_{rule_code}", lang=lang)


def get_risk_label(risk_level: str, lang: str = "ar") -> str:
    """Translate risk severity levels."""
    return t(f"risk_{risk_level.upper()}", lang=lang)


def localize_columns(df: pd.DataFrame, lang: str) -> pd.DataFrame:
    """Return a copy of the DataFrame with localized column headers for display."""
    if lang not in COLUMN_LABELS or df.empty:
        return df
    return df.rename(columns=COLUMN_LABELS[lang])


def get_direction_css(lang: str = "ar") -> str:
    """Return CSS for RTL layout in Arabic and LTR layout in English/French."""
    if lang == "ar":
        return """
        <style>
            .stApp, .stSidebar, [data-testid="stAppViewContainer"] {
                direction: rtl;
                text-align: right;
            }
            [data-testid="stMetricValue"], [data-testid="stMetricDelta"] {
                direction: ltr;
                text-align: right;
            }
            .stDataFrame {
                direction: rtl;
            }
        </style>
        """
    return """
    <style>
        .stApp, .stSidebar, [data-testid="stAppViewContainer"] {
            direction: ltr;
            text-align: left;
        }
    </style>
    """


def sanitize_workspace_slug(raw_name: str) -> str:
    """Convert any workspace name into a safe filesystem slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_name.strip()).strip("_").lower()
    return cleaned[:48] if cleaned else "default"


def resolve_workspace_database_url(workspace_slug: str) -> str:
    """Build an isolated SQLite database URL inside data/workspaces/."""
    workspaces_dir = ROOT_DIR / "data" / "workspaces"
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    db_file = (workspaces_dir / f"ws_{workspace_slug}.db").resolve()
    return f"sqlite:///{db_file.as_posix()}"


# ---------------------------------------------------------------------------
# Page Setup, Session/Workspace Isolation & Sidebar Controls
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Invoice Spend & Anomaly Hub", layout="wide")

if "session_workspace_id" not in st.session_state:
    st.session_state["session_workspace_id"] = f"sess_{uuid.uuid4().hex[:10]}"

try:
    default_api_key = st.secrets.get("GEMINI_API_KEY", getattr(settings, "GEMINI_API_KEY", ""))
except Exception:
    default_api_key = getattr(settings, "GEMINI_API_KEY", "")

default_spike_pct = float(getattr(settings, "PRICE_SPIKE_THRESHOLD_PCT", 25.0))
default_dup_days = int(getattr(settings, "NEAR_DUPLICATE_DAYS", 7))
default_proc_threshold = float(getattr(settings, "PROCUREMENT_THRESHOLD", 3500000.0))

with st.sidebar:
    lang = st.selectbox(
        "🌐 اللغة / Language / Langue",
        options=list(SUPPORTED_LANGUAGES.keys()),
        index=0,
        format_func=lambda code: SUPPORTED_LANGUAGES[code],
    )
    st.divider()
    st.subheader(t("sidebar_workspace_session", lang))
    ws_mode = st.radio(
        t("workspace_mode", lang),
        options=["isolated", "named"],
        format_func=lambda m: t("mode_isolated_session", lang) if m == "isolated" else t("mode_named_workspace", lang),
        label_visibility="collapsed",
    )

    if ws_mode == "isolated":
        active_ws_slug = st.session_state["session_workspace_id"]
        if st.button(t("new_isolated_session_btn", lang), use_container_width=True):
            st.session_state["session_workspace_id"] = f"sess_{uuid.uuid4().hex[:10]}"
            st.rerun()
    else:
        raw_ws_name = st.text_input(t("workspace_name_input", lang), value="audit_team_1")
        active_ws_slug = sanitize_workspace_slug(raw_ws_name)

    st.caption(t("active_workspace_badge", lang, ws=f"`{active_ws_slug}`"))

    workspace_db_url = resolve_workspace_database_url(active_ws_slug)
    engine = build_engine(workspace_db_url)
    init_schema(engine)
    SessionLocal = build_session_factory(engine)

    st.divider()
    st.header(t("sidebar_settings", lang))
    api_key_input = st.text_input(t("gemini_api_key", lang), value=default_api_key, type="password")
    spike_pct = st.slider(
        t("price_spike_pct", lang),
        min_value=5.0,
        max_value=100.0,
        value=default_spike_pct,
        step=5.0,
    )
    dup_days = st.slider(
        t("near_duplicate_days", lang),
        min_value=1,
        max_value=30,
        value=default_dup_days,
        step=1,
    )
    proc_threshold = st.number_input(
        t("procurement_threshold", lang),
        min_value=10000.0,
        max_value=100000000.0,
        value=default_proc_threshold,
        step=100000.0,
    )

    st.divider()
    st.subheader(t("sidebar_warehouse", lang))
    if st.button(t("reset_warehouse_btn", lang), type="secondary", use_container_width=True):
        reset_warehouse(engine)
        st.success(t("reset_warehouse_success", lang))
        st.rerun()

    st.divider()
    st.subheader(t("sidebar_rule_weights", lang))
    active_weights: Dict[str, int] = {}
    for rule_key, default_w in DEFAULT_RULE_WEIGHTS.items():
        label = get_rule_label(rule_key, lang)
        desc = get_rule_description(rule_key, lang)
        enabled = st.checkbox(label, value=True, key=f"rule_en_{rule_key}", help=desc)
        if enabled:
            w_val = st.slider(
                t("rule_weight", lang, label=label),
                min_value=0,
                max_value=60,
                value=int(default_w),
                step=5,
                key=f"rule_w_{rule_key}",
            )
            active_weights[rule_key] = int(w_val)
        else:
            active_weights[rule_key] = 0

st.markdown(get_direction_css(lang), unsafe_allow_html=True)
st.title(t("app_title", lang))
st.caption(t("app_subtitle", lang))

# ---------------------------------------------------------------------------
# Section 1: Invoice Ingestion (Multi-File Batch + Smart Deduplication)
# ---------------------------------------------------------------------------
st.subheader(t("section_ingest", lang))
uploaded_files = st.file_uploader(
    t("upload_label", lang),
    type=["pdf", "csv", "xlsx"],
    accept_multiple_files=True,
    help=t("upload_help", lang),
)
if uploaded_files and st.button(t("process_btn", lang), type="primary"):
    try:
        results = []
        duplicate_warnings = []
        errors = []
        seen_hashes_in_batch: Dict[str, str] = {}
        total_files = len(uploaded_files)
        progress_bar = st.progress(0.0)
        status_text = st.empty()

        with SessionLocal() as session:
            # Pre-load existing file hashes from the warehouse for instant duplicate detection
            existing_hashes: Set[str] = {
                row[0] for row in session.query(FactInvoice.file_sha256).all() if row[0]
            }

            for file_idx, uploaded in enumerate(uploaded_files, start=1):
                status_text.info(
                    t("processing_file", lang, current=file_idx, total=total_files, filename=uploaded.name)
                )
                try:
                    file_bytes = uploaded.getvalue()
                    base_hash = compute_sha256(file_bytes)
                    lower_name = uploaded.name.lower()

                    # 1. Check if the exact file was already selected in the same upload batch
                    if base_hash in seen_hashes_in_batch:
                        duplicate_warnings.append(
                            t(
                                "duplicate_in_batch",
                                lang,
                                filename=uploaded.name,
                                original=seen_hashes_in_batch[base_hash],
                            )
                        )
                        progress_bar.progress(file_idx / total_files)
                        continue

                    # 2. Check if the exact file already exists in the warehouse before calling Gemini API
                    if base_hash in existing_hashes:
                        duplicate_warnings.append(
                            t("duplicate_in_warehouse", lang, filename=uploaded.name)
                        )
                        seen_hashes_in_batch[base_hash] = uploaded.name
                        progress_bar.progress(file_idx / total_files)
                        continue

                    seen_hashes_in_batch[base_hash] = uploaded.name

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
                        existing_hashes.add(base_hash)
                        results.append(res)
                    else:
                        batch = parse_tabular_invoices(file_bytes, uploaded.name)
                        for idx, (invoice, lines) in enumerate(batch, start=1):
                            row_hash = compute_sha256(
                                f"{base_hash}:{idx}:{invoice.vendor_name}:{invoice.invoice_number}".encode("utf-8")
                            )
                            if row_hash in existing_hashes:
                                continue
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
                            existing_hashes.add(row_hash)
                            results.append(res)
                except Exception as file_exc:
                    errors.append(t("file_error", lang, filename=uploaded.name, err=file_exc))

                progress_bar.progress(file_idx / total_files)

        status_text.empty()
        if results:
            processed_files_count = len(seen_hashes_in_batch) - len(
                [w for w in duplicate_warnings if "warehouse" in w or "المستودع" in w or "l'entrepôt" in w]
            )
            st.success(t("upload_success", lang, count=len(results), files=max(processed_files_count, 1)))
            st.json(results)
        if duplicate_warnings:
            st.warning(t("duplicate_summary", lang, skipped=len(duplicate_warnings)))
            for warn_msg in duplicate_warnings:
                st.caption(f"• {warn_msg}")
        for err_msg in errors:
            st.error(err_msg)
    except Exception as exc:
        st.error(f"{t('error_prefix', lang)}: {exc}")

# ---------------------------------------------------------------------------
# Section 2: Spend & Risk Overview
# ---------------------------------------------------------------------------
st.divider()
st.subheader(t("section_overview", lang))

df_invoices = fetch_invoices_dataframe(engine)
if df_invoices.empty:
    st.info(t("no_data_yet", lang))
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(t("kpi_total_invoices", lang), len(df_invoices))
    c2.metric(t("kpi_total_spend", lang), f"{df_invoices['grand_total_amount'].sum():,.2f}")
    c3.metric(t("kpi_high_risk", lang), int((df_invoices["risk_level"] == "HIGH").sum()))
    c4.metric(t("kpi_total_discrepancy", lang), f"{df_invoices['discrepancy_amount'].abs().sum():,.2f}")

    vendor_df = fetch_vendor_spend_dataframe(engine)
    if not vendor_df.empty:
        col_v1, col_v2 = st.columns([2, 1])
        with col_v1:
            st.markdown(f"**{t('spend_by_vendor', lang)}**")
            st.bar_chart(vendor_df.set_index("vendor_name")["total_spend"])
        with col_v2:
            st.markdown(f"**{t('vendor_risk_summary', lang)}**")
            st.dataframe(localize_columns(vendor_df, lang), use_container_width=True)

    risk_filter = st.multiselect(
        t("filter_risk_level", lang),
        options=["LOW", "MEDIUM", "HIGH"],
        default=["LOW", "MEDIUM", "HIGH"],
        format_func=lambda lvl: get_risk_label(lvl, lang),
    )
    filtered_df = df_invoices[df_invoices["risk_level"].isin(risk_filter)] if risk_filter else df_invoices
    st.dataframe(localize_columns(filtered_df, lang), use_container_width=True)

    selected_key = st.selectbox(
        t("inspect_invoice_key", lang),
        options=filtered_df["invoice_key"].tolist(),
    )
    if selected_key is not None:
        row_match = filtered_df[filtered_df["invoice_key"] == selected_key]
        if not row_match.empty:
            st.info(f"{t('audit_notes_prefix', lang)}: {row_match.iloc[0]['summary_notes']}")
        st.markdown(f"**{t('line_items_deviation', lang)}**")
        lines_df = fetch_lines_for_invoice(engine, int(selected_key))
        st.dataframe(localize_columns(lines_df, lang), use_container_width=True)

    # -----------------------------------------------------------------------
    # Section 3: Clean Exports
    # -----------------------------------------------------------------------
    st.subheader(t("section_exports", lang))
    col_e1, col_e2, col_e3 = st.columns(3)
    col_e1.download_button(
        label=t("export_csv", lang),
        data=filtered_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="invoices_intelligence.csv",
        mime="text/csv",
    )
    excel_buf = BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
        filtered_df.to_excel(writer, index=False, sheet_name="Invoices")
    col_e2.download_button(
        label=t("export_excel", lang),
        data=excel_buf.getvalue(),
        file_name="invoices_intelligence.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    col_e3.download_button(
        label=t("export_json", lang),
        data=filtered_df.to_json(orient="records", indent=2).encode("utf-8"),
        file_name="invoices_intelligence.json",
        mime="application/json",
    )