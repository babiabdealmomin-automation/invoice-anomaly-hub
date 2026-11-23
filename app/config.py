import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite"
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/anomaly_hub.db").strip()
    PRICE_SPIKE_THRESHOLD_PCT: float = float(os.getenv("PRICE_SPIKE_THRESHOLD_PCT", "25.0"))
    NEAR_DUPLICATE_DAYS: int = int(os.getenv("NEAR_DUPLICATE_DAYS", "7"))


settings = Settings()
