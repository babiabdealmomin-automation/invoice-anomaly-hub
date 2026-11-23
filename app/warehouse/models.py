from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from sqlalchemy import Column, Date, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class DimVendor(Base):
    __tablename__ = "dim_vendors"

    vendor_key = Column(Integer, primary_key=True, autoincrement=True)
    canonical_name = Column(String(255), unique=True, nullable=False, index=True)
    tax_identifier = Column(String(100), nullable=True)
    default_currency = Column(String(20), nullable=False, default="USD")

    invoices = relationship("FactInvoice", back_populates="vendor")


class DimDate(Base):
    __tablename__ = "dim_dates"

    date_key = Column(Integer, primary_key=True)
    full_date = Column(Date, unique=True, nullable=False)
    year = Column(Integer, nullable=False)
    quarter = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    day = Column(Integer, nullable=False)

    invoices = relationship("FactInvoice", back_populates="invoice_date")


class FactInvoice(Base):
    __tablename__ = "fact_invoices"

    invoice_key = Column(Integer, primary_key=True, autoincrement=True)
    vendor_key = Column(Integer, ForeignKey("dim_vendors.vendor_key"), nullable=False, index=True)
    date_key = Column(Integer, ForeignKey("dim_dates.date_key"), nullable=False, index=True)
    invoice_number = Column(String(100), nullable=False)
    file_sha256 = Column(String(64), unique=True, nullable=False, index=True)
    source_filename = Column(String(255), nullable=False)
    currency = Column(String(20), nullable=False, default="USD")

    subtotal_amount = Column(Float, nullable=False, default=0.0)
    tax_amount = Column(Float, nullable=False, default=0.0)
    discount_amount = Column(Float, nullable=False, default=0.0)
    grand_total_amount = Column(Float, nullable=False, default=0.0)
    expected_total = Column(Float, nullable=False, default=0.0)
    discrepancy_amount = Column(Float, nullable=False, default=0.0)

    audit_status = Column(String(30), nullable=False)
    risk_score = Column(Integer, nullable=False, default=0)
    risk_level = Column(String(20), nullable=False, default="LOW")
    risk_flags = Column(Text, nullable=False, default="")
    summary_notes = Column(Text, nullable=False, default="")

    vendor = relationship("DimVendor", back_populates="invoices")
    invoice_date = relationship("DimDate", back_populates="invoices")
    lines = relationship("FactInvoiceLine", back_populates="invoice", cascade="all, delete-orphan")


class FactInvoiceLine(Base):
    __tablename__ = "fact_invoice_lines"

    line_key = Column(Integer, primary_key=True, autoincrement=True)
    invoice_key = Column(Integer, ForeignKey("fact_invoices.invoice_key"), nullable=False, index=True)
    line_number = Column(Integer, nullable=False)
    item_description = Column(Text, nullable=False)
    normalized_item = Column(String(255), nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    unit_price = Column(Float, nullable=False)
    line_total = Column(Float, nullable=False)
    expected_line_total = Column(Float, nullable=False)
    is_line_valid = Column(Integer, nullable=False, default=1)
    historical_avg_price = Column(Float, nullable=True)
    price_deviation_pct = Column(Float, nullable=True)
    is_price_anomaly = Column(Integer, nullable=False, default=0)

    invoice = relationship("FactInvoice", back_populates="lines")


def _normalize_database_url(database_url: str) -> str:
    raw = (database_url or "").strip()
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if raw.startswith("postgresql://") and "+psycopg2" not in raw and "+" not in raw.split("://", 1)[1].split("/", 1)[0]:
        raw = raw.replace("postgresql://", "postgresql+psycopg2://", 1)

    if raw.startswith("postgresql"):
        parsed = urlparse(raw)
        query_params = dict(parse_qsl(parsed.query))
        if "sslmode" not in query_params:
            query_params["sslmode"] = "require"
        raw = urlunparse(parsed._replace(query=urlencode(query_params)))
    return raw


def build_engine(database_url: str):
    normalized_url = _normalize_database_url(database_url)
    if normalized_url.startswith("sqlite"):
        return create_engine(normalized_url, connect_args={"check_same_thread": False})
    return create_engine(normalized_url, pool_pre_ping=True)


def build_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
