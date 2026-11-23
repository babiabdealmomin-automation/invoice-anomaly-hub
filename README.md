# Invoice Spend & Anomaly Intelligence Hub

Deterministic invoice auditing, spend warehousing (`Star Schema`), and financial anomaly detection (`ARITHMETIC_MISMATCH`, `PRICE_SPIKE`, `NEAR_DUPLICATE_INVOICE`) with **Streamlit** dashboard and `CSV` / `Excel` / `JSON` exports.

## Quick Start
```bash
cd C:\projects\invoice-anomaly-hub
copy .env.example .env
python -m pip install -r requirements.txt
set PYTHONPATH=.
python -m pytest -v
python -m streamlit run app\ui\streamlit_app.py
