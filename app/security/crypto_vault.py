"""
Cryptographic security layer for Invoice Spend & Anomaly Intelligence Hub:
1. Encryption at Rest:
   - Uses `cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256 authenticated
     encryption) with per-workspace PBKDF2-HMAC-SHA256 key derivation.
   - Encrypts sensitive text fields (e.g., accountant reviewer_note) before
     writing to SQLite, and encrypts original uploaded documents on disk (.enc).
2. Encryption in Transit:
   - Enforces TLS/SSL parameters on external remote database URLs.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

ENC_PREFIX = "ENC::"
DEFAULT_MASTER_SEED = "babi-ai-invoice-hub-default-at-rest-seed-v1"


class DecryptionIntegrityError(ValueError):
    """Raised when encrypted bytes fail Fernet HMAC/AES verification."""


def _get_master_secret_bytes(master_secret: Optional[str] = None) -> bytes:
    """Resolve the application master encryption key from argument, st.secrets, or env."""
    if master_secret and master_secret.strip():
        return master_secret.strip().encode("utf-8")

    try:
        import streamlit as st

        if "WORKSPACE_MASTER_KEY" in st.secrets and str(st.secrets["WORKSPACE_MASTER_KEY"]).strip():
            return str(st.secrets["WORKSPACE_MASTER_KEY"]).strip().encode("utf-8")
    except Exception:
        pass

    env_secret = os.getenv("WORKSPACE_MASTER_KEY", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")

    return DEFAULT_MASTER_SEED.encode("utf-8")


def derive_workspace_fernet(
    workspace_slug: str = "default",
    master_secret: Optional[str] = None,
) -> Fernet:
    """
    Derive a unique 32-byte URL-safe Fernet key (AES-128-CBC + HMAC-SHA256)
    bound to the specific `workspace_slug` via PBKDF2-HMAC-SHA256.
    """
    salt = hashlib.sha256(f"ws-salt::{workspace_slug}".encode("utf-8")).digest()[:16]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=120_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(_get_master_secret_bytes(master_secret)))
    return Fernet(key)


def _is_known_plaintext_document(data: bytes) -> bool:
    """
    Detect legacy unencrypted files (PDF, XLSX/ZIP, PNG, JPEG, or valid UTF-8 CSV)
    so backward compatibility works without passing corrupted ciphertext as valid files.
    """
    if not data:
        return False
    # Known binary magic headers
    if data.startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return True
    # Fernet tokens always begin with version byte 0x80 ('gAAAAA' in base64)
    if data.startswith(b"gAAAAA"):
        return False
    # Check if valid UTF-8 CSV/text
    try:
        sample = data[:4096].decode("utf-8")
        return any(sep in sample for sep in (",", ";", "\t", "\n"))
    except UnicodeDecodeError:
        return False


def encrypt_sensitive_text(
    plaintext: str,
    workspace_slug: str = "default",
    master_secret: Optional[str] = None,
) -> str:
    """Encrypt a sensitive string field before storing it in the database."""
    if not plaintext or not plaintext.strip():
        return ""
    if plaintext.startswith(ENC_PREFIX):
        return plaintext
    fernet = derive_workspace_fernet(workspace_slug=workspace_slug, master_secret=master_secret)
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{ENC_PREFIX}{token}"


def decrypt_sensitive_text(
    ciphertext: str,
    workspace_slug: str = "default",
    master_secret: Optional[str] = None,
) -> str:
    """
    Decrypt a previously encrypted database text field.
    Safely returns legacy plaintext values unchanged if not prefixed with ENC::.
    """
    if not ciphertext:
        return ""
    if not ciphertext.startswith(ENC_PREFIX):
        return ciphertext
    token = ciphertext[len(ENC_PREFIX) :].encode("ascii")
    fernet = derive_workspace_fernet(workspace_slug=workspace_slug, master_secret=master_secret)
    try:
        return fernet.decrypt(token).decode("utf-8")
    except InvalidToken:
        return "[🔒 محتوى مشفّر — تعذّر فك التشفير بمفتاح الجلسة الحالي]"


def encrypt_file_bytes(
    raw_bytes: bytes,
    workspace_slug: str = "default",
    master_secret: Optional[str] = None,
) -> bytes:
    """Encrypt binary document bytes (PDF, CSV, XLSX, Images) before writing to disk."""
    fernet = derive_workspace_fernet(workspace_slug=workspace_slug, master_secret=master_secret)
    return fernet.encrypt(raw_bytes)


def decrypt_file_bytes(
    encrypted_bytes: bytes,
    workspace_slug: str = "default",
    master_secret: Optional[str] = None,
) -> bytes:
    """
    Decrypt binary document bytes read from disk.
    - If Fernet decryption succeeds, returns verified plaintext bytes.
    - If InvalidToken occurs, allows only verified legacy plaintext formats;
      otherwise logs a clear security warning and raises DecryptionIntegrityError
      so corrupted/wrong-key ciphertext is never served as a valid file.
    """
    fernet = derive_workspace_fernet(workspace_slug=workspace_slug, master_secret=master_secret)
    try:
        return fernet.decrypt(encrypted_bytes)
    except InvalidToken as exc:
        if _is_known_plaintext_document(encrypted_bytes):
            logger.info(
                "Serving legacy unencrypted attachment for workspace '%s'.",
                workspace_slug,
            )
            return encrypted_bytes

        logger.warning(
            "Fernet decryption failed (InvalidToken) for workspace '%s': "
            "possible wrong WORKSPACE_MASTER_KEY or tampered/corrupted file.",
            workspace_slug,
        )
        raise DecryptionIntegrityError(
            "فشل فك تشفير الملف: مفتاح التشفير غير مطابق أو الملف تالف."
        ) from exc


def enforce_tls_database_url(db_url: str) -> str:
    """
    Ensure Encryption in Transit (TLS/SSL) is enforced whenever connecting
    to an external remote SQL database (PostgreSQL / MySQL).
    Local SQLite URLs are returned unchanged.
    """
    url = (db_url or "").strip()
    if not url or url.startswith("sqlite"):
        return url

    if url.startswith(("postgresql://", "postgres://")) and "sslmode=" not in url:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}sslmode=require"

    if url.startswith("mysql") and "ssl" not in url.lower():
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}ssl_ca=/etc/ssl/certs/ca-certificates.crt"

    return url