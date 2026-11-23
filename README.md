# Invoice Spend & Anomaly Intelligence Hub

[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-Interactive%20UI-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-Star%20Schema-D71F00)](https://www.sqlalchemy.org/)
[![Gemini File API](https://img.shields.io/badge/Google%20Gemini-File%20API-4285F4?logo=googlegemini&logoColor=white)](https://ai.google.dev/)
[![Pytest](https://img.shields.io/badge/Tests-Pytest-0A9EDC?logo=pytest&logoColor=white)](https://docs.pytest.org/)

منظومة تحليل وتدقيق فواتير ومصروفات مبنية على **Python 3.14** و **Streamlit** و **SQLAlchemy / SQLite** و **Google Gemini File API**، تجمع بين الاستخراج المباشر للمستندات (`PDF`، `CSV`، `XLSX`)، والتدقيق الحسابي الحتمي، وكشف الأنماط الشاذة ومؤشرات المخاطر العالية، وتخزين البيانات في مستودع تحليلي بمخطط نجمة (`Star Schema`) جاهز للتصدير والربط ضمن **BABI.AI**.

---

## نظرة عامة على المعمارية

ينقسم المشروع إلى أربع طبقات معيارية واضحة لتسهيل الصيانة والاختبار والتوسع:

| الطبقة | المسار | الدور الوظيفي |
| :--- | :--- | :--- |
| **الاستخراج (`Extraction`)** | `app/extraction/` | قراءة الفواتير بصيغة `PDF` مباشرة عبر `Gemini File API`، واستيراد ملفات `CSV` و `XLSX` وتجميع البنود حسب الفاتورة والمورّد، وحساب `SHA-256` لمنع التكرار الحرفي. |
| **الذكاء والتدقيق (`Intelligence`)** | `app/intelligence/audit_engine.py` | محرك تدقيق حسابي حتمي وتقييم للمخاطر (`0–100`) وتصنيفها (`LOW` / `MEDIUM` / `HIGH`) وفق أوزان قابلة للتخصيص أو التعطيل. |
| **المستودع (`Warehouse`)** | `app/warehouse/` | نماذج `SQLAlchemy` ومستودع البيانات بمخطط نجمة (`dim_vendors`، `dim_dates`، `fact_invoices`، `fact_invoice_lines`) مع استعلامات تحليلية وإعادة تهيئة آمنة. |
| **الواجهة (`UI`)** | `app/ui/streamlit_app.py` | لوحة تحكم تفاعلية لإدارة العتبات، وتخصيص أوزان القواعد، وإعادة تهيئة المستودع، واستعراض المؤشرات والبنود، وتصدير التقارير (`CSV`، `XLSX`، `JSON`). |

---

## قواعد التدقيق ومؤشرات المخاطر

يطبّق المحرك فحصاً حسابياً صارماً (`quantity * unit_price = line_total` ومطابقة `Subtotal` والضرائب والخصومات مع `Grand Total`) إلى جانب قواعد كشف الشذوذ والمخاطر:

- **`ARITHMETIC_MISMATCH`:** رصد أي فرق حسابي في بنود الفاتورة أو الإجماليات.
- **`PRICE_SPIKE`:** اكتشاف ارتفاع سعر الوحدة عن المتوسط التاريخي لنفس المورّد بنسبة تتجاوز العتبة المحددة (`Price Spike Threshold %`).
- **`NEAR_DUPLICATE_INVOICE`:** رصد فواتير متقاربة في المبلغ والتاريخ لنفس المورّد ضمن النافذة الزمنية المحددة.
- **`SPLIT_INVOICE_PATTERN`:** رصد احتمالية تجزئة الفواتير ضمن سقف المشتريات المعتمد (`Procurement Threshold`) خلال النافذة الزمنية.
- **`HIGH_RISK_RED_FLAGS`:** رصد المؤشرات النصية عالية الخطورة في البنود أو الشروط (مثل غياب السندات/الأوامر، أو التعيينات العامة، أو الاستعجال غير المبرر).
- **`THIRD_PARTY_OR_OFFSHORE_PAYEE`:** تنبيه الدفع لطرف ثالث أو حسابات/جهات `Offshore` أو مناطق حرة.
- **`ROUND_HIGH_VALUE_TOTAL`:** رصد المبالغ الإجمالية الكبيرة المستديرة.
- **`NIF_MISMATCH` / `INVALID_NIF_FORMAT` / `BANK_ACCOUNT_CHANGED`:** التحقق من صيغة المعرف الجبائي (`NIF`) وتغيّر المعرف أو الحساب البنكي للمورّد.

---

## هيكل المستودع

```text
invoice-anomaly-hub/
├── app/
│   ├── config.py
│   ├── schemas.py
│   ├── extraction/
│   │   ├── ai_parser.py
│   │   ├── pdf_reader.py
│   │   └── tabular_importer.py
│   ├── intelligence/
│   │   └── audit_engine.py
│   ├── ui/
│   │   └── streamlit_app.py
│   └── warehouse/
│       ├── models.py
│       └── repository.py
├── tests/
│   └── test_engine.py
├── .env.example
├── .gitignore
├── README.md
└── requirements.txt
```

---

## الإعداد والتشغيل

### 1. إعداد متغيرات البيئة
انسخ ملف `.env.example` إلى `.env` وحدّث القيم حسب بيئتك:

```powershell
Set-Location -Path "C:\projects\invoice-anomaly-hub"
Copy-Item .env.example .env
```

المتغيرات المعتمدة في `.env.example`:
- `GEMINI_API_KEY`: مفتاح واجهة `Google Gemini API` لمعالجة ملفات `PDF`.
- `GEMINI_MODEL`: نموذج `Gemini` المعتمد للاستخراج.
- `DATABASE_URL`: مسار قاعدة بيانات `SQLite` (الافتراضي: `sqlite:///./data/anomaly_hub.db`).
- `PRICE_SPIKE_THRESHOLD_PCT`: النسبة المئوية الافتراضية لقفزات الأسعار (الافتراضي: `25.0`).
- `NEAR_DUPLICATE_DAYS`: النافذة الزمنية بالأيام للفواتير شبه المكررة وتجزئة الفواتير (الافتراضي: `7`).
- `PROCUREMENT_THRESHOLD`: سقف المشتريات المرجعي بالدينار الجزائري (الافتراضي: `3500000.0`).

### 2. تشغيل اختبارات الوحدة (`Pytest`)

```powershell
Set-Location -Path "C:\projects\invoice-anomaly-hub"
$env:PYTHONPATH = "C:\projects\invoice-anomaly-hub"
& "C:\Python314\python.exe" -m pytest -v
```

### 3. تشغيل لوحة التحكم (`Streamlit`)

```powershell
Set-Location -Path "C:\projects\invoice-anomaly-hub"
$env:PYTHONPATH = "C:\projects\invoice-anomaly-hub"
& "C:\Python314\python.exe" -m streamlit run app\ui\streamlit_app.py
```

---

## التكامل مع BABI.AI

يُعد هذا المشروع أحد مشاريع **BABI.AI** في مجال هندسة البيانات وتحليل المخاطر المالية، ويمكن ربطه مباشرة عبر المستودع:
`https://github.com/babiabdealmomin-automation/invoice-anomaly-hub`
