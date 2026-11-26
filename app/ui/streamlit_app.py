"""
Streamlit interactive dashboard for Invoice Spend & Anomaly Intelligence Hub.
Self-contained multilingual UI (Arabic with mobile-safe RTL, English, French) and
Streamlit Community Cloud sys.path compatibility.
"""

import re
import sys
import uuid
from datetime import date
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
from app.warehouse.db_hardening import (
    configure_sqlite_wal_and_timeout,
    ensure_composite_audit_indexes,
    normalize_and_validate_dataframe,
)
from app.warehouse.repository import (
    fetch_invoices_dataframe,
    fetch_lines_for_invoice,
    fetch_vendor_spend_dataframe,
    init_schema,
    ingest_invoice_record,
    reset_warehouse,
)

# ---------------------------------------------------------------------------
# Multi-Currency & Regional Tax/Approval Profiles (SAR, AED, USD, EUR, DZD...)
# ---------------------------------------------------------------------------
CURRENCY_PROFILES: Dict[str, Dict[str, object]] = {
    "SAR": {"symbol": "SAR", "default_vat": 15.0, "default_approval_limit": 100000.0, "step": 5000.0},
    "AED": {"symbol": "AED", "default_vat": 5.0, "default_approval_limit": 100000.0, "step": 5000.0},
    "USD": {"symbol": "USD", "default_vat": 0.0, "default_approval_limit": 25000.0, "step": 1000.0},
    "EUR": {"symbol": "EUR", "default_vat": 20.0, "default_approval_limit": 25000.0, "step": 1000.0},
    "DZD": {"symbol": "DZD", "default_vat": 19.0, "default_approval_limit": 3500000.0, "step": 100000.0},
    "EGP": {"symbol": "EGP", "default_vat": 14.0, "default_approval_limit": 500000.0, "step": 25000.0},
    "MAD": {"symbol": "MAD", "default_vat": 20.0, "default_approval_limit": 250000.0, "step": 10000.0},
    "TND": {"symbol": "TND", "default_vat": 19.0, "default_approval_limit": 75000.0, "step": 5000.0},
}

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
        "api_key_server_active": "🔒 مفتاح الخادم مفعّل ومعزول بأمان",
        "api_key_missing": "⚠️ مفتاح API غير مضبوط في إعدادات الخادم (Secrets)",
        "reporting_currency": "💱 عملة العرض والتقييم المالي",
        "vat_rate_pct": "نسبة ضريبة القيمة المضافة القياسية (VAT %)",
        "vat_tolerance_pct": "هامش التسامح في مطابقة الضريبة (%)",
        "price_spike_pct": "عتبة ارتفاع سعر الوحدة (%)",
        "near_duplicate_days": "نافذة رصد التكرار والتجزئة (بالأيام)",
        "procurement_threshold": "سقف الاعتماد المالي المباشر ({currency})",
        "sidebar_warehouse": "🗄️ صيانة مستودع البيانات",
        "reset_warehouse_btn": "🗑️ إعادة تهيئة المستودع",
        "reset_warehouse_success": "تم تفريغ المستودع وإعادة تهيئته بنجاح.",
        "sidebar_rule_weights": "⚖️ أوزان وتفعيل قواعد التدقيق المحاسبي",
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
        "kpi_total_spend": "إجمالي المصروفات ({currency})",
        "kpi_high_risk": "فواتير تتطلب مراجعة عاجلة",
        "kpi_total_discrepancy": "إجمالي الفروقات الحسابية ({currency})",
        "vat_audit_warning": "⚠️ تنبيه ضريبي: تم رصد {count} فاتورة يختلف فيها مبلغ ضريبة القيمة المضافة عن النسبة القياسية ({vat}%).",
        "future_date_warning": "📅 تنبيه واقعية التاريخ: تم رصد {count} فاتورة تحمل تاريخ إصدار مستقبلي يتعدى اليوم الحالي ({today}).",
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
        "reset_defaults_btn": "🔄 استعادة الإعدادات الافتراضية",
        "reset_defaults_done": "تمت استعادة جميع العتبات والأوزان الافتراضية.",
        "risk_LOW": "منخفض (LOW)",
        "risk_MEDIUM": "متوسط (MEDIUM)",
        "risk_HIGH": "مرتفع (HIGH)",
        "rule_ARITHMETIC_MISMATCH": "فرق في المطابقة الحسابية",
        "rule_PRICE_SPIKE": "ارتفاع غير معتاد في سعر الوحدة",
        "rule_NEAR_DUPLICATE_INVOICE": "اشتباه تكرار فاتورة لنفس المورّد",
        "rule_SPLIT_INVOICE_PATTERN": "اشتباه تجزئة المشتريات لتجاوز سقف الاعتماد",
        "rule_HIGH_RISK_RED_FLAGS": "مؤشرات رقابية تستوجب التحقق المستندي",
        "rule_THIRD_PARTY_OR_OFFSHORE_PAYEE": "تحويل الدفع لطرف ثالث أو حساب خارجي",
        "rule_ROUND_HIGH_VALUE_TOTAL": "مبلغ إجمالي مرتفع ومقرّب بالكامل",
        "rule_NIF_MISMATCH": "اختلاف الرقم الضريبي عن سجل المورّد",
        "rule_INVALID_NIF_FORMAT": "صيغة الرقم الضريبي غير مكتملة أو غير صحيحة",
        "rule_BANK_ACCOUNT_CHANGED": "تغيير بيانات الحساب البنكي للمورّد",
        "rule_NON_POSITIVE_GRAND_TOTAL": "فاتورة بإجمالي صفري أو سالب",
        "desc_ARITHMETIC_MISMATCH": "عدم تطابق ناتج (الكمية × سعر الوحدة) مع إجمالي البند، أو اختلاف المجموع الفرعي وضريبة القيمة المضافة والخصم عن الإجمالي النهائي.",
        "desc_PRICE_SPIKE": "تجاوز سعر الوحدة لمتوسط الأسعار التاريخية لنفس المورّد بنسبة تفوق الحد المسموح به.",
        "desc_NEAR_DUPLICATE_INVOICE": "وجود فاتورة أخرى لنفس المورّد بمبلغ متقارب جداً خلال نافذة الأيام المحددة، مما يستدعي التأكد من عدم الازدواج.",
        "desc_SPLIT_INVOICE_PATTERN": "تعدد الفواتير الصادرة من نفس المورّد خلال فترة وجيزة بما يوحي بتقسيم معاملة واحدة للبقاء دون سقف الاعتماد المالي.",
        "desc_HIGH_RISK_RED_FLAGS": "ورود مؤشرات في وصف البنود أو الشروط تستدعي التدقيق (مثل غياب أمر الشراء، أو عمومية الوصف، أو طلب الصرف العاجل دون مبرر).",
        "desc_THIRD_PARTY_OR_OFFSHORE_PAYEE": "توجيه السداد إلى مستفيد يختلف عن المورّد الأصلي أو إلى حسابات خارجية / مناطق حرة.",
        "desc_ROUND_HIGH_VALUE_TOTAL": "قيمة الفاتورة مرتفعة ومقرّبة لأرقام صحيحة بالكامل، مما يتطلب مراجعة التسعير الفعلي والكميات.",
        "desc_NIF_MISMATCH": "الرقم الضريبي المذكور في الفاتورة يختلف عن الرقم الضريبي المعتمد مسبقاً في ملف المورّد.",
        "desc_INVALID_NIF_FORMAT": "الرقم الضريبي المسجل لا يطابق الصيغة النظامية المعتمدة (عدد الخانات أو البنية).",
        "desc_BANK_ACCOUNT_CHANGED": "تغيّر رقم الحساب البنكي (IBAN / رقم الحساب) مقارنة ببيانات السداد السابقة للمورّد.",
        "desc_NON_POSITIVE_GRAND_TOTAL": "المبلغ الإجمالي للفاتورة يساوي صفراً أو يحمل قيمة سالبة تتطلب تسوية.",
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
        "api_key_server_active": "🔒 Server secret active & isolated",
        "api_key_missing": "⚠️ Server API key not configured in Secrets",
        "reporting_currency": "💱 Reporting & Audit Currency",
        "vat_rate_pct": "Standard VAT Rate (%)",
        "vat_tolerance_pct": "VAT Reconciliation Tolerance (%)",
        "price_spike_pct": "Unit Price Increase Threshold (%)",
        "near_duplicate_days": "Duplicate & Split Detection Window (Days)",
        "procurement_threshold": "Direct Approval Limit ({currency})",
        "sidebar_warehouse": "🗄️ Warehouse Maintenance",
        "reset_warehouse_btn": "🗑️ Reset Warehouse",
        "reset_warehouse_success": "Warehouse cleared and reinitialized.",
        "sidebar_rule_weights": "⚖️ Audit Rule Weights & Toggles",
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
        "kpi_total_spend": "Total Spend ({currency})",
        "kpi_high_risk": "High-Priority Review",
        "kpi_total_discrepancy": "Total Math Variance ({currency})",
        "vat_audit_warning": "⚠️ VAT Alert: {count} invoice(s) show a VAT amount deviating from the configured {vat}% standard rate.",
        "future_date_warning": "📅 Date Validity Alert: {count} invoice(s) have a future issue date beyond today ({today}).",
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
        "reset_defaults_btn": "🔄 Reset to Default Settings",
        "reset_defaults_done": "All thresholds and rule weights restored to defaults.",
        "risk_LOW": "LOW",
        "risk_MEDIUM": "MEDIUM",
        "risk_HIGH": "HIGH",
        "rule_ARITHMETIC_MISMATCH": "Calculation Variance",
        "rule_PRICE_SPIKE": "Unusual Unit Price Increase",
        "rule_NEAR_DUPLICATE_INVOICE": "Potential Duplicate Invoice",
        "rule_SPLIT_INVOICE_PATTERN": "Potential Purchase Splitting",
        "rule_HIGH_RISK_RED_FLAGS": "Supporting Document / Policy Alert",
        "rule_THIRD_PARTY_OR_OFFSHORE_PAYEE": "Third-Party or Cross-Border Payee",
        "rule_ROUND_HIGH_VALUE_TOTAL": "High-Value Round Amount",
        "rule_NIF_MISMATCH": "Tax ID Mismatch",
        "rule_INVALID_NIF_FORMAT": "Invalid Tax ID Format",
        "rule_BANK_ACCOUNT_CHANGED": "Vendor Bank Account Changed",
        "rule_NON_POSITIVE_GRAND_TOTAL": "Zero or Negative Invoice Total",
        "desc_ARITHMETIC_MISMATCH": "Line total does not match (quantity × unit price), or subtotal, VAT, and discounts do not reconcile with the grand total.",
        "desc_PRICE_SPIKE": "Unit price exceeds the vendor's historical average by more than the configured threshold percentage.",
        "desc_NEAR_DUPLICATE_INVOICE": "Another invoice from the same vendor with a closely matching amount was issued within the configured day window.",
        "desc_SPLIT_INVOICE_PATTERN": "Multiple invoices from the same vendor within a short window appear split to stay below the direct approval limit.",
        "desc_HIGH_RISK_RED_FLAGS": "Commercial terms or line descriptions indicate missing purchase orders, vague deliverables, or unsupported urgency.",
        "desc_THIRD_PARTY_OR_OFFSHORE_PAYEE": "Payment beneficiary differs from the contracting vendor or directs funds to an offshore/free-zone account.",
        "desc_ROUND_HIGH_VALUE_TOTAL": "Large invoice total ending in exact round figures, warranting price and quantity verification.",
        "desc_NIF_MISMATCH": "Tax registration number on the invoice differs from the vendor's verified tax ID on file.",
        "desc_INVALID_NIF_FORMAT": "Tax registration number does not follow the required standard format.",
        "desc_BANK_ACCOUNT_CHANGED": "Vendor bank account details (IBAN / account number) changed compared to prior approved invoices.",
        "desc_NON_POSITIVE_GRAND_TOTAL": "Invoice grand total is zero or negative and requires accounting review.",
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
        "api_key_server_active": "🔒 Clé serveur active et isolée",
        "api_key_missing": "⚠️ Clé API serveur non configurée dans Secrets",
        "reporting_currency": "💱 Devise d'Audit et d'Affichage",
        "vat_rate_pct": "Taux standard de TVA (%)",
        "vat_tolerance_pct": "Tolérance d'écart TVA (%)",
        "price_spike_pct": "Seuil de hausse du prix unitaire (%)",
        "near_duplicate_days": "Fenêtre doublons / fractionnement (jours)",
        "procurement_threshold": "Plafond d'approbation directe ({currency})",
        "sidebar_warehouse": "🗄️ Maintenance de l'Entrepôt",
        "reset_warehouse_btn": "🗑️ Réinitialiser l'Entrepôt",
        "reset_warehouse_success": "L'entrepôt a été vidé et réinitialisé avec succès.",
        "sidebar_rule_weights": "⚖️ Pondération des Règles d'Audit",
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
        "kpi_total_spend": "Dépenses Totales ({currency})",
        "kpi_high_risk": "Révision Prioritaire",
        "kpi_total_discrepancy": "Écart Arithmétique Total ({currency})",
        "vat_audit_warning": "⚠️ Alerte TVA : {count} facture(s) présentent un montant de TVA s'écartant du taux standard de {vat}%.",
        "future_date_warning": "📅 Alerte Validité de Date : {count} facture(s) portent une date d'émission future dépassant aujourd'hui ({today}).",
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
        "reset_defaults_btn": "🔄 Restaurer les paramètres par défaut",
        "reset_defaults_done": "Tous les seuils et poids des règles ont été restaurés.",
        "risk_LOW": "FAIBLE (LOW)",
        "risk_MEDIUM": "MOYEN (MEDIUM)",
        "risk_HIGH": "ÉLEVÉ (HIGH)",
        "rule_ARITHMETIC_MISMATCH": "Écart de calcul comptable",
        "rule_PRICE_SPIKE": "Hausse inhabituelle du prix unitaire",
        "rule_NEAR_DUPLICATE_INVOICE": "Suspicion de facture en double",
        "rule_SPLIT_INVOICE_PATTERN": "Suspicion de fractionnement d'achat",
        "rule_HIGH_RISK_RED_FLAGS": "Alerte justificatifs ou conformité commerciale",
        "rule_THIRD_PARTY_OR_OFFSHORE_PAYEE": "Bénéficiaire tiers ou compte étranger",
        "rule_ROUND_HIGH_VALUE_TOTAL": "Montant élevé parfaitement arrondi",
        "rule_NIF_MISMATCH": "Divergence d'identifiant fiscal",
        "rule_INVALID_NIF_FORMAT": "Format d'identifiant fiscal invalide",
        "rule_BANK_ACCOUNT_CHANGED": "Changement de coordonnées bancaires",
        "rule_NON_POSITIVE_GRAND_TOTAL": "Total facture nul ou négatif",
        "desc_ARITHMETIC_MISMATCH": "Le total ligne ne correspond pas à (quantité × prix unitaire), ou le total HT, la TVA et la remise ne concordent pas avec le TTC.",
        "desc_PRICE_SPIKE": "Le prix unitaire dépasse la moyenne historique du fournisseur au-delà du pourcentage autorisé.",
        "desc_NEAR_DUPLICATE_INVOICE": "Une autre facture du même fournisseur avec un montant très proche a été émise dans la fenêtre de jours configurée.",
        "desc_SPLIT_INVOICE_PATTERN": "Plusieurs factures du même fournisseur sur une courte période suggèrent un fractionnement pour rester sous le plafond d'approbation.",
        "desc_HIGH_RISK_RED_FLAGS": "Les conditions commerciales ou libellés signalent une absence de bon de commande, une description vague ou une urgence non justifiée.",
        "desc_THIRD_PARTY_OR_OFFSHORE_PAYEE": "Le règlement est adressé à un tiers distinct du fournisseur ou vers une juridiction offshore / zone franche.",
        "desc_ROUND_HIGH_VALUE_TOTAL": "Montant total élevé se terminant par des chiffres ronds, nécessitant une vérification des prix et quantités.",
        "desc_NIF_MISMATCH": "L'identifiant fiscal figurant sur la facture diffère de celui enregistré dans la fiche fournisseur.",
        "desc_INVALID_NIF_FORMAT": "L'identifiant fiscal ne respecte pas le format réglementaire attendu.",
        "desc_BANK_ACCOUNT_CHANGED": "Les coordonnées bancaires (IBAN / RIB) ont changé par rapport aux factures précédentes.",
        "desc_NON_POSITIVE_GRAND_TOTAL": "Le montant total TTC est nul ou négatif et nécessite une régularisation comptable.",
    },
}

COLUMN_LABELS: Dict[str, Dict[str, str]] = {
    "ar": {
        "invoice_key": "رمز الفاتورة",
        "invoice_number": "رقم الفاتورة",
        "vendor_name": "اسم المورّد",
        "vendor_nif": "الرقم الضريبي للمورّد",
        "invoice_date": "تاريخ الفاتورة",
        "issue_date": "تاريخ الإصدار",
        "due_date": "تاريخ الاستحقاق",
        "created_at": "تاريخ التسجيل",
        "source_filename": "اسم الملف المصدر",
        "currency": "العملة",
        "subtotal_amount": "المجموع قبل الضريبة",
        "tax_amount": "ضريبة القيمة المضافة",
        "discount_amount": "الخصم",
        "grand_total_amount": "المبلغ الإجمالي",
        "expected_grand_total": "الإجمالي المتوقع",
        "discrepancy_amount": "الفرق الحسابي",
        "risk_score": "نقاط المخاطر",
        "risk_level": "مستوى الخطورة",
        "summary_notes": "ملاحظات التدقيق المحاسبي",
        "status": "الحالة",
        "total_spend": "إجمالي المصروفات",
        "invoice_count": "عدد الفواتير",
        "avg_risk_score": "متوسط المخاطر",
        "total_discrepancy": "إجمالي الفروقات",
        "line_number": "رقم البند",
        "line_key": "رمز البند",
        "description": "البيان / الوصف",
        "quantity": "الكمية",
        "unit_price": "سعر الوحدة",
        "historical_avg_price": "متوسط السعر التاريخي",
        "line_total": "إجمالي البند",
        "expected_line_total": "الإجمالي المتوقع للبند",
        "price_deviation_pct": "نسبة انحراف السعر (%)",
    },
    "fr": {
        "invoice_key": "Clé Facture",
        "invoice_number": "N° Facture",
        "vendor_name": "Fournisseur",
        "vendor_nif": "Identifiant Fiscal",
        "invoice_date": "Date Facture",
        "issue_date": "Date d'Émission",
        "due_date": "Date d'Échéance",
        "created_at": "Date d'Enregistrement",
        "source_filename": "Fichier Source",
        "currency": "Devise",
        "subtotal_amount": "Montant HT",
        "tax_amount": "TVA",
        "discount_amount": "Remise",
        "grand_total_amount": "Montant TTC",
        "expected_grand_total": "Total Attendu",
        "discrepancy_amount": "Écart",
        "risk_score": "Score Risque",
        "risk_level": "Niveau Risque",
        "summary_notes": "Notes d'Audit Comptable",
        "status": "Statut",
        "total_spend": "Dépenses Totales",
        "invoice_count": "Nb Factures",
        "avg_risk_score": "Risque Moyen",
        "total_discrepancy": "Écart Total",
        "line_number": "N° Ligne",
        "line_key": "Clé Ligne",
        "description": "Désignation",
        "quantity": "Quantité",
        "unit_price": "Prix Unitaire",
        "historical_avg_price": "Prix Moyen Historique",
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
    """Return a copy of the DataFrame with localized headers and cell labels for display."""
    if df.empty:
        return df
    display_df = df.copy()
    if "risk_level" in display_df.columns:
        display_df["risk_level"] = display_df["risk_level"].apply(
            lambda v: get_risk_label(str(v), lang) if pd.notna(v) and str(v).strip() else "—"
        )
    if lang not in COLUMN_LABELS:
        return display_df
    return display_df.rename(columns=COLUMN_LABELS[lang])


def localize_audit_notes(
    note_text: object,
    lang: str = "ar",
    row_context: Dict[str, object] | None = None,
    expected_vat_pct: float | None = None,
    vat_tolerance_pct: float = 1.0,
) -> str:
    """
    Translate engine-generated audit summary notes and enrich alerts with explicit
    accounting details: violation type, expected vs. actual value, and variance amount.
    """
    ctx = row_context or {}
    currency = str(ctx.get("currency") or "").strip()
    subtotal = pd.to_numeric(ctx.get("subtotal_amount"), errors="coerce")
    tax_amt = pd.to_numeric(ctx.get("tax_amount"), errors="coerce")
    grand_total = pd.to_numeric(ctx.get("grand_total_amount"), errors="coerce")
    disc_amt = pd.to_numeric(ctx.get("discrepancy_amount"), errors="coerce")

    raw = "" if (note_text is None or (isinstance(note_text, float) and pd.isna(note_text))) else str(note_text).strip()

    # Build explicit accounting variance breakdown when math, VAT, or future-date discrepancies exist
    accounting_details: list[str] = []

    raw_issue_date = ctx.get("issue_date") if pd.notna(ctx.get("issue_date")) else ctx.get("invoice_date")
    if raw_issue_date is not None and pd.notna(raw_issue_date) and str(raw_issue_date).strip():
        parsed_dt = pd.to_datetime(raw_issue_date, errors="coerce")
        today_dt = date.today()
        if pd.notna(parsed_dt) and parsed_dt.date() > today_dt:
            days_ahead = (parsed_dt.date() - today_dt).days
            inv_dt_str = parsed_dt.date().isoformat()
            today_str = today_dt.isoformat()
            if lang == "ar":
                accounting_details.append(
                    f"• نوع المخالفة: تاريخ إصدار غير واقعي (مستقبلي) | الحد الأقصى المسموح: {today_str} | "
                    f"التاريخ المسجّل في الفاتورة: {inv_dt_str} | مقدار التجاوز: +{days_ahead} يوم"
                )
            elif lang == "fr":
                accounting_details.append(
                    f"• Type d'anomalie : Date d'émission future irréaliste | Date limite autorisée : {today_str} | "
                    f"Date figurant sur la facture : {inv_dt_str} | Dépassement : +{days_ahead} jour(s)"
                )
            else:
                accounting_details.append(
                    f"• Violation Type: Unrealistic Future Issue Date | Max Allowed Date: {today_str} | "
                    f"Invoice Issue Date: {inv_dt_str} | Variance: +{days_ahead} day(s)"
                )

    if pd.notna(disc_amt) and abs(float(disc_amt)) >= 0.01 and pd.notna(grand_total):
        actual_gt = float(grand_total)
        expected_gt = actual_gt - float(disc_amt)
        variance_val = float(disc_amt)
        if lang == "ar":
            accounting_details.append(
                f"• نوع المخالفة: فرق في المطابقة الحسابية | القيمة المتوقعة: {expected_gt:,.2f} {currency} | "
                f"القيمة الفعلية: {actual_gt:,.2f} {currency} | مقدار الفارق: {variance_val:+,.2f} {currency}"
            )
        elif lang == "fr":
            accounting_details.append(
                f"• Type d'anomalie : Écart arithmétique | Valeur attendue : {expected_gt:,.2f} {currency} | "
                f"Valeur réelle : {actual_gt:,.2f} {currency} | Écart : {variance_val:+,.2f} {currency}"
            )
        else:
            accounting_details.append(
                f"• Violation Type: Calculation Variance | Expected Value: {expected_gt:,.2f} {currency} | "
                f"Actual Value: {actual_gt:,.2f} {currency} | Variance: {variance_val:+,.2f} {currency}"
            )

    if (
        expected_vat_pct is not None
        and pd.notna(subtotal)
        and float(subtotal) > 0
        and pd.notna(tax_amt)
    ):
        sub_val = float(subtotal)
        actual_vat_amt = float(tax_amt)
        actual_vat_pct = (actual_vat_amt / sub_val) * 100.0
        if abs(actual_vat_pct - float(expected_vat_pct)) > float(vat_tolerance_pct):
            expected_vat_amt = sub_val * (float(expected_vat_pct) / 100.0)
            vat_diff = actual_vat_amt - expected_vat_amt
            if lang == "ar":
                accounting_details.append(
                    f"• نوع المخالفة: اختلاف نسبة ضريبة القيمة المضافة ({actual_vat_pct:.2f}% بدلاً من {expected_vat_pct:g}%) | "
                    f"الضريبة المتوقعة: {expected_vat_amt:,.2f} {currency} | الضريبة الفعلية: {actual_vat_amt:,.2f} {currency} | "
                    f"مقدار الفارق: {vat_diff:+,.2f} {currency}"
                )
            elif lang == "fr":
                accounting_details.append(
                    f"• Type d'anomalie : Écart de taux de TVA ({actual_vat_pct:.2f}% au lieu de {expected_vat_pct:g}%) | "
                    f"TVA attendue : {expected_vat_amt:,.2f} {currency} | TVA réelle : {actual_vat_amt:,.2f} {currency} | "
                    f"Écart : {vat_diff:+,.2f} {currency}"
                )
            else:
                accounting_details.append(
                    f"• Violation Type: VAT Rate Deviation ({actual_vat_pct:.2f}% vs. {expected_vat_pct:g}%) | "
                    f"Expected VAT: {expected_vat_amt:,.2f} {currency} | Actual VAT: {actual_vat_amt:,.2f} {currency} | "
                    f"Variance: {vat_diff:+,.2f} {currency}"
                )

    if lang == "ar":
        replacements = [
            (r"(?i)all deterministic (?:audit )?checks passed\.?", "اجتازت الفاتورة جميع فحوصات المطابقة الحسابية والرقابية بنجاح."),
            (r"(?i)all checks passed\.?", "اجتازت الفاتورة جميع الفحوصات بنجاح."),
            (r"(?i)passed all deterministic checks\.?", "اجتازت الفاتورة جميع فحوصات المطابقة الحسابية بنجاح."),
            (r"(?i)no anomalies(?: or red flags)? detected\.?", "لم يتم رصد أي انحرافات أو مؤشرات مخاطر."),
            (r"(?i)no issues found\.?", "لا توجد ملاحظات أو مخالفات محاسبية."),
            (r"(?i)clean invoice\.?", "فاتورة سليمة ومطابقة محاسبياً."),
            (r"(?i)arithmetic mismatch", "فرق في المطابقة الحسابية"),
            (r"(?i)price spike", "ارتفاع غير معتاد في سعر الوحدة"),
            (r"(?i)near[- ]duplicate invoice", "اشتباه تكرار فاتورة لنفس المورّد"),
            (r"(?i)split[- ]invoice pattern", "اشتباه تجزئة المشتريات لتجاوز سقف الاعتماد"),
            (r"(?i)high[- ]risk red flags", "مؤشرات رقابية تستوجب التحقق المستندي"),
            (r"(?i)third[- ]party or offshore payee", "تحويل الدفع لطرف ثالث أو حساب خارجي"),
            (r"(?i)round high[- ]value total", "مبلغ إجمالي مرتفع ومقرّب بالكامل"),
            (r"(?i)nif mismatch", "اختلاف الرقم الضريبي عن سجل المورّد"),
            (r"(?i)invalid nif format", "صيغة الرقم الضريبي غير مكتملة أو غير صحيحة"),
            (r"(?i)bank account changed", "تغيير بيانات الحساب البنكي للمورّد"),
            (r"(?i)non[- ]positive grand total", "فاتورة بإجمالي صفري أو سالب"),
            (r"(?i)discrepancy of", "بفارق حسابي قدره"),
            (r"(?i)expected", "القيمة المتوقعة"),
            (r"(?i)actual", "القيمة الفعلية"),
            (r"(?i)exceeds historical average by", "يتجاوز متوسط السعر التاريخي بنسبة"),
            (r"(?i)within\s+(\d+)\s+days", r"خلال \1 يوم/أيام"),
        ]
        translated = raw
        for pattern, repl in replacements:
            translated = re.sub(pattern, repl, translated)
        if not translated and not accounting_details:
            return "اجتازت الفاتورة جميع فحوصات المطابقة الحسابية والرقابية بنجاح."
    elif lang == "fr":
        replacements_fr = [
            (r"(?i)all deterministic (?:audit )?checks passed\.?", "Tous les contrôles arithmétiques et réglementaires ont réussi."),
            (r"(?i)all checks passed\.?", "Tous les contrôles ont réussi."),
            (r"(?i)no anomalies(?: or red flags)? detected\.?", "Aucune anomalie détectée."),
            (r"(?i)clean invoice\.?", "Facture conforme."),
            (r"(?i)arithmetic mismatch", "Écart de calcul comptable"),
            (r"(?i)price spike", "Hausse inhabituelle du prix unitaire"),
            (r"(?i)near[- ]duplicate invoice", "Suspicion de facture en double"),
            (r"(?i)split[- ]invoice pattern", "Suspicion de fractionnement d'achat"),
        ]
        translated = raw
        for pattern, repl in replacements_fr:
            translated = re.sub(pattern, repl, translated)
        if not translated and not accounting_details:
            return "Tous les contrôles arithmétiques et réglementaires ont réussi."
    else:
        translated = raw or "All deterministic checks passed."

    if accounting_details:
        details_block = "\n".join(accounting_details)
        if translated and "اجتازت الفاتورة" not in translated and "passed" not in translated.lower():
            return f"{details_block}\n{translated}"
        return details_block

    return translated


def get_direction_css(lang: str = "ar") -> str:
    """
    Mobile-safe responsive CSS:
    - Fixes Streamlit's collapsed sidebar overlap bug on mobile/RTL browsers by
      explicitly translating collapsed sidebars completely off-screen.
    - Applies RTL to content blocks and sidebar inner contents cleanly.
    - 2x2 responsive KPI grid on mobile screens without number truncation.
    - Touch-friendly spacing for filter tags/pills.
    """
    content_dir = "direction: rtl; text-align: right;" if lang == "ar" else "direction: ltr; text-align: left;"
    metric_align = "right" if lang == "ar" else "left"
    return f"""
    <style>
        /* Hide Streamlit deploy badge and default footer while keeping sidebar toggle accessible */
        #MainMenu {{visibility: hidden;}}
        footer {{visibility: hidden;}}
        .stDeployButton {{display: none !important;}}

        /* CRITICAL FIX: Force collapsed sidebar completely off-screen on mobile & RTL */
        section[data-testid="stSidebar"][aria-expanded="false"] {{
            transform: translateX(-110%) !important;
            margin-left: 0 !important;
            margin-right: 0 !important;
            width: 0 !important;
            min-width: 0 !important;
            max-width: 0 !important;
            overflow: hidden !important;
            visibility: hidden !important;
            pointer-events: none !important;
        }}

        section[data-testid="stSidebar"][aria-expanded="true"] {{
            transform: translateX(0) !important;
            visibility: visible !important;
            pointer-events: auto !important;
            z-index: 999999 !important;
        }}

        /* Apply RTL/LTR safely to inner content containers */
        [data-testid="stMainBlockContainer"],
        [data-testid="stSidebarUserContent"] {{
            {content_dir}
        }}

        [data-testid="stMainBlockContainer"] h1,
        [data-testid="stMainBlockContainer"] h2,
        [data-testid="stMainBlockContainer"] h3,
        [data-testid="stMainBlockContainer"] p,
        [data-testid="stSidebarUserContent"] p,
        [data-testid="stSidebarUserContent"] label {{
            {content_dir}
        }}

        /* Prevent metric number truncation and enforce crisp typography */
        [data-testid="stMetric"] {{
            background: rgba(128, 128, 128, 0.06);
            border: 1px solid rgba(128, 128, 128, 0.18);
            border-radius: 12px;
            padding: 0.85rem 1rem;
        }}
        [data-testid="stMetricLabel"] p {{
            white-space: normal !important;
            overflow: visible !important;
            text-overflow: unset !important;
            font-size: 0.9rem !important;
        }}
        [data-testid="stMetricValue"] {{
            direction: ltr;
            text-align: {metric_align};
            white-space: nowrap !important;
            overflow: visible !important;
            text-overflow: unset !important;
            font-size: clamp(1.05rem, 2.2vw, 1.55rem) !important;
        }}

        /* Mobile & in-app browser optimization */
        @media (max-width: 768px) {{
            section[data-testid="stSidebar"][aria-expanded="true"] {{
                width: 85vw !important;
                max-width: 340px !important;
            }}
            [data-testid="stMainBlockContainer"] {{
                padding-left: 0.95rem !important;
                padding-right: 0.95rem !important;
                padding-top: 2.5rem !important;
                max-width: 100vw !important;
            }}
            [data-testid="stMainBlockContainer"] h1 {{
                font-size: 1.45rem !important;
                line-height: 1.35 !important;
                word-break: break-word !important;
            }}
            [data-testid="stMainBlockContainer"] h2,
            [data-testid="stMainBlockContainer"] h3 {{
                font-size: 1.12rem !important;
                line-height: 1.35 !important;
            }}
            [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {{
                display: grid !important;
                grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
                gap: 0.65rem !important;
            }}
            [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) > div {{
                width: 100% !important;
                min-width: 0 !important;
            }}
        }}

        /* Touch-friendly spacing for multiselect filter tags / pills */
        [data-baseweb="tag"] {{
            margin: 4px 6px !important;
            padding: 4px 10px !important;
            border-radius: 8px !important;
            font-size: 0.9rem !important;
        }}
        [data-baseweb="select"] > div {{
            gap: 6px !important;
            padding-top: 4px !important;
            padding-bottom: 4px !important;
        }}
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
    return f"sqlite:///{db_file.as_posix()}?timeout=30.0"


def _get_server_gemini_key() -> str:
    """Read the server-side Gemini API key strictly inside backend memory (never sent to UI)."""
    try:
        secret_val = st.secrets.get("GEMINI_API_KEY", "")
        if secret_val:
            return str(secret_val).strip()
    except Exception:
        pass
    return str(getattr(settings, "GEMINI_API_KEY", "") or "").strip()


def sanitize_error_message(err: object) -> str:
    """Redact any accidental API key or secret tokens from exception messages shown to users."""
    msg = str(err)
    msg = re.sub(r"AIza[0-9A-Za-z\-_]{20,}", "[REDACTED_API_KEY]", msg)
    msg = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)['\"]?[^\s'\"]+['\"]?", r"\1[REDACTED]", msg)
    return msg


def evaluate_issue_date_realism(invoice_obj: object, lang: str = "ar") -> tuple[bool, str]:
    """
    Validate that the invoice issue date does not exceed the current date.
    Returns (is_future_date, localized_warning_message).
    """
    raw_dt = getattr(invoice_obj, "issue_date", None) or getattr(invoice_obj, "invoice_date", None)
    if raw_dt is None:
        return False, ""
    parsed_dt = pd.to_datetime(raw_dt, errors="coerce")
    today_dt = date.today()
    if pd.isna(parsed_dt) or parsed_dt.date() <= today_dt:
        return False, ""

    days_ahead = (parsed_dt.date() - today_dt).days
    inv_dt_str = parsed_dt.date().isoformat()
    today_str = today_dt.isoformat()
    inv_num = getattr(invoice_obj, "invoice_number", "") or "N/A"
    if lang == "ar":
        msg = (
            f"الفاتورة ({inv_num}): تاريخ الإصدار ({inv_dt_str}) يتعدى اليوم الحالي ({today_str}) "
            f"بفارق (+{days_ahead} يوم)."
        )
    elif lang == "fr":
        msg = (
            f"Facture ({inv_num}) : la date d'émission ({inv_dt_str}) dépasse la date du jour ({today_str}) "
            f"de (+{days_ahead} jour(s))."
        )
    else:
        msg = (
            f"Invoice ({inv_num}): issue date ({inv_dt_str}) exceeds today's date ({today_str}) "
            f"by (+{days_ahead} day(s))."
        )
    return True, msg


def enforce_future_date_audit_flag(session, invoice_key: int | None, is_future: bool) -> None:
    """Escalate persisted invoice risk score/level if its issue date is in the future."""
    if not is_future or invoice_key is None:
        return
    record = session.query(FactInvoice).filter(FactInvoice.invoice_key == int(invoice_key)).first()
    if record is None:
        return
    current_score = float(getattr(record, "risk_score", 0.0) or 0.0)
    record.risk_score = min(100.0, max(current_score + 35.0, 75.0))
    record.risk_level = "HIGH"
    existing_notes = str(getattr(record, "summary_notes", "") or "").strip()
    flag_note = "Future issue date exceeds current date."
    if flag_note not in existing_notes:
        record.summary_notes = f"{existing_notes} | {flag_note}".strip(" |")
    session.commit()
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
    configure_sqlite_wal_and_timeout(engine, timeout_seconds=30.0)
    init_schema(engine)
    ensure_composite_audit_indexes(engine)
    SessionLocal = build_session_factory(engine)

    # 2. Collapsible Audit Thresholds & Currency Configuration
    with st.expander(t("sidebar_settings", lang), expanded=True):
        effective_api_key = _get_server_gemini_key()
        if effective_api_key:
            st.caption(t("api_key_server_active", lang))
        else:
            st.warning(t("api_key_missing", lang))

        selected_currency = st.selectbox(
            t("reporting_currency", lang),
            options=list(CURRENCY_PROFILES.keys()),
            index=0,
            key="cfg_currency",
        )
        curr_profile = CURRENCY_PROFILES[selected_currency]

        vat_rate_pct = st.number_input(
            t("vat_rate_pct", lang),
            min_value=0.0,
            max_value=50.0,
            value=float(curr_profile["default_vat"]),
            step=0.5,
            key=f"vat_rate_{selected_currency}",
        )
        vat_tolerance_pct = st.slider(
            t("vat_tolerance_pct", lang),
            min_value=0.0,
            max_value=5.0,
            value=1.0,
            step=0.5,
            key="cfg_vat_tol",
        )

        spike_pct = st.slider(
            t("price_spike_pct", lang),
            min_value=5.0,
            max_value=100.0,
            value=default_spike_pct,
            step=5.0,
            key="cfg_spike_pct",
        )
        dup_days = st.slider(
            t("near_duplicate_days", lang),
            min_value=1,
            max_value=30,
            value=default_dup_days,
            step=1,
            key="cfg_dup_days",
        )
        proc_threshold = st.number_input(
            t("procurement_threshold", lang, currency=selected_currency),
            min_value=100.0,
            max_value=1000000000.0,
            value=float(curr_profile["default_approval_limit"]),
            step=float(curr_profile["step"]),
            key=f"approval_limit_{selected_currency}",
        )

    # 3. Collapsible Audit Rule Weights & Toggles
    with st.expander(t("sidebar_rule_weights", lang), expanded=False):
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

    # 4. Collapsible Warehouse Maintenance
    with st.expander(t("sidebar_warehouse", lang), expanded=False):
        if st.button(t("reset_warehouse_btn", lang), type="secondary", use_container_width=True):
            reset_warehouse(engine)
            ensure_composite_audit_indexes(engine)
            st.success(t("reset_warehouse_success", lang))
            st.rerun()

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
        date_warnings = []
        errors = []
        seen_hashes_in_batch: Dict[str, str] = {}
        total_files = len(uploaded_files)
        progress_bar = st.progress(0.0)
        status_text = st.empty()

        with SessionLocal() as session:
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

                    if base_hash in existing_hashes:
                        duplicate_warnings.append(
                            t("duplicate_in_warehouse", lang, filename=uploaded.name)
                        )
                        seen_hashes_in_batch[base_hash] = uploaded.name
                        progress_bar.progress(file_idx / total_files)
                        continue

                    seen_hashes_in_batch[base_hash] = uploaded.name

                    if lower_name.endswith(".pdf"):
                        invoice, lines = parse_invoice_pdf_native(file_bytes, api_key=effective_api_key)
                        is_future_dt, dt_warn = evaluate_issue_date_realism(invoice, lang=lang)
                        if is_future_dt:
                            date_warnings.append(dt_warn)
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
                        if is_future_dt and isinstance(res, dict):
                            enforce_future_date_audit_flag(session, res.get("invoice_key"), True)
                            res["risk_level"] = "HIGH"
                            res["future_issue_date_alert"] = dt_warn
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
                            is_future_dt, dt_warn = evaluate_issue_date_realism(invoice, lang=lang)
                            if is_future_dt:
                                date_warnings.append(dt_warn)
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
                            if is_future_dt and isinstance(res, dict):
                                enforce_future_date_audit_flag(session, res.get("invoice_key"), True)
                                res["risk_level"] = "HIGH"
                                res["future_issue_date_alert"] = dt_warn
                            existing_hashes.add(row_hash)
                            results.append(res)
                except Exception as file_exc:
                    errors.append(
                        t("file_error", lang, filename=uploaded.name, err=sanitize_error_message(file_exc))
                    )

                progress_bar.progress(file_idx / total_files)

        status_text.empty()
        if results:
            processed_files_count = len(seen_hashes_in_batch) - len(
                [w for w in duplicate_warnings if "warehouse" in w or "المستودع" in w or "l'entrepôt" in w]
            )
            st.success(t("upload_success", lang, count=len(results), files=max(processed_files_count, 1)))
            st.json(results)
        if date_warnings:
            st.error(t("future_date_warning", lang, count=len(date_warnings), today=date.today().isoformat()))
            for d_warn in date_warnings:
                st.caption(f"• {d_warn}")
        if duplicate_warnings:
            st.warning(t("duplicate_summary", lang, skipped=len(duplicate_warnings)))
            for warn_msg in duplicate_warnings:
                st.caption(f"• {warn_msg}")
        for err_msg in errors:
            st.error(err_msg)
    except Exception as exc:
        st.error(f"{t('error_prefix', lang)}: {sanitize_error_message(exc)}")

# ---------------------------------------------------------------------------
# Section 2: Spend & Risk Overview
# ---------------------------------------------------------------------------
st.divider()
st.subheader(t("section_overview", lang))

raw_invoices_df = fetch_invoices_dataframe(engine)
df_invoices = normalize_and_validate_dataframe(
    raw_invoices_df,
    required_columns=[
        "invoice_key",
        "invoice_number",
        "vendor_name",
        "invoice_date",
        "currency",
        "subtotal_amount",
        "tax_amount",
        "grand_total_amount",
        "discrepancy_amount",
        "risk_score",
        "risk_level",
        "summary_notes",
    ],
    numeric_columns=["subtotal_amount", "tax_amount", "grand_total_amount", "discrepancy_amount", "risk_score"],
)

if df_invoices.empty:
    st.info(t("no_data_yet", lang))
else:
    df_invoices["currency"] = df_invoices["currency"].fillna(selected_currency).replace("", selected_currency)
    df_invoices["summary_notes"] = df_invoices.apply(
        lambda row: localize_audit_notes(
            row.get("summary_notes"),
            lang=lang,
            row_context=row.to_dict(),
            expected_vat_pct=float(vat_rate_pct),
            vat_tolerance_pct=float(vat_tolerance_pct),
        ),
        axis=1,
    )

    total_spend_val = df_invoices["grand_total_amount"].sum(skipna=True)
    total_disc_val = df_invoices["discrepancy_amount"].abs().sum(skipna=True)
    high_risk_count = int((df_invoices["risk_level"].fillna("").astype(str).str.upper() == "HIGH").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(t("kpi_total_invoices", lang), len(df_invoices))
    c2.metric(
        t("kpi_total_spend", lang, currency=selected_currency),
        f"{total_spend_val:,.2f} {selected_currency}" if pd.notna(total_spend_val) else "—",
    )
    c3.metric(t("kpi_high_risk", lang), high_risk_count)
    c4.metric(
        t("kpi_total_discrepancy", lang, currency=selected_currency),
        f"{total_disc_val:,.2f} {selected_currency}" if pd.notna(total_disc_val) else "—",
    )

    date_col_name = "issue_date" if "issue_date" in df_invoices.columns and df_invoices["issue_date"].notna().any() else "invoice_date"
    if date_col_name in df_invoices.columns:
        parsed_dates = pd.to_datetime(df_invoices[date_col_name], errors="coerce")
        today_dt = date.today()
        future_date_rows = df_invoices[parsed_dates.notna() & (parsed_dates.dt.date > today_dt)]
        if not future_date_rows.empty:
            st.error(t("future_date_warning", lang, count=len(future_date_rows), today=today_dt.isoformat()))

    valid_vat_rows = df_invoices.dropna(subset=["subtotal_amount", "tax_amount"])
    valid_vat_rows = valid_vat_rows[valid_vat_rows["subtotal_amount"] > 0]
    if not valid_vat_rows.empty:
        effective_vat_pct = (valid_vat_rows["tax_amount"] / valid_vat_rows["subtotal_amount"]) * 100.0
        vat_mismatches = valid_vat_rows[(effective_vat_pct - vat_rate_pct).abs() > vat_tolerance_pct]
        if not vat_mismatches.empty:
            st.warning(t("vat_audit_warning", lang, count=len(vat_mismatches), vat=f"{vat_rate_pct:g}"))

    raw_vendor_df = fetch_vendor_spend_dataframe(engine)
    vendor_df = normalize_and_validate_dataframe(
        raw_vendor_df,
        required_columns=["vendor_name", "total_spend"],
        numeric_columns=["total_spend", "avg_risk_score", "total_discrepancy"],
    )
    if not vendor_df.empty and "vendor_name" in vendor_df.columns and "total_spend" in vendor_df.columns:
        col_v1, col_v2 = st.columns([2, 1])
        with col_v1:
            st.markdown(f"**{t('spend_by_vendor', lang)}**")
            chart_df = vendor_df.dropna(subset=["vendor_name", "total_spend"])
            if not chart_df.empty:
                st.bar_chart(chart_df.set_index("vendor_name")["total_spend"])
        with col_v2:
            st.markdown(f"**{t('vendor_risk_summary', lang)}**")
            localized_vendor_df = localize_columns(vendor_df, lang)
            vendor_col_cfg = {
                col: st.column_config.TextColumn(width="medium")
                for col in localized_vendor_df.columns
            }
            st.dataframe(localized_vendor_df, use_container_width=True, column_config=vendor_col_cfg)

    risk_filter = st.multiselect(
        t("filter_risk_level", lang),
        options=["LOW", "MEDIUM", "HIGH"],
        default=["LOW", "MEDIUM", "HIGH"],
        format_func=lambda lvl: get_risk_label(lvl, lang),
    )
    normalized_risk_series = df_invoices["risk_level"].fillna("").astype(str).str.upper()
    filtered_df = df_invoices[normalized_risk_series.isin(risk_filter)] if risk_filter else df_invoices
    localized_invoices_df = localize_columns(filtered_df, lang)
    invoice_col_cfg = {
        col: st.column_config.TextColumn(width="medium")
        for col in localized_invoices_df.columns
    }
    st.dataframe(localized_invoices_df, use_container_width=True, column_config=invoice_col_cfg)

    valid_keys = filtered_df["invoice_key"].dropna().tolist()
    selected_key = st.selectbox(
        t("inspect_invoice_key", lang),
        options=valid_keys,
    ) if valid_keys else None

    if selected_key is not None:
        row_match = filtered_df[filtered_df["invoice_key"] == selected_key]
        if not row_match.empty:
            notes_val = row_match.iloc[0].get("summary_notes", "")
            st.info(f"{t('audit_notes_prefix', lang)}: {notes_val if pd.notna(notes_val) else '—'}")
        st.markdown(f"**{t('line_items_deviation', lang)}**")
        raw_lines_df = fetch_lines_for_invoice(engine, int(selected_key))
        lines_df = normalize_and_validate_dataframe(
            raw_lines_df,
            required_columns=["description", "quantity", "unit_price", "line_total"],
            numeric_columns=["quantity", "unit_price", "line_total", "expected_line_total", "price_deviation_pct"],
        )
        localized_lines_df = localize_columns(lines_df, lang)
        lines_col_cfg = {
            col: st.column_config.TextColumn(width="medium")
            for col in localized_lines_df.columns
        }
        st.dataframe(localized_lines_df, use_container_width=True, column_config=lines_col_cfg)

    # ---------------------------------------------------------------------------
    # Section 3: Clean Exports
    # ---------------------------------------------------------------------------
    st.divider()
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