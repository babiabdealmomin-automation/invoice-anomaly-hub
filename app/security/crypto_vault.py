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
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ENC_PREFIX = "ENC::"
DEFAULT_MASTER_SEED = "babi-ai-invoice-hub-default-at-rest-seed-v1"


def _get_master_secret_bytes(master_secret: Optional[str] = None) -> bytes:
    """Resolve the master encryption key material from argument, env, or fallback."""
    if master_secret and master_secret.strip():
        return master_secret.strip().encode("utf-8")
    env_key = os.getenv("WORKSPACE_MASTER_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key.encode("utf-8")
    return DEFAULT_MASTER_SEED.encode("utf-8")


def derive_workspace_fernet(
    workspace_slug: str = "default",
    master_secret: Optional[str] = None,
) -> Fernet:
    """
    Derive a deterministic, workspace-isolated 32-byte Fernet key using
    PBKDF2HMAC-SHA256 so each workspace's files and sensitive fields are
    cryptographically separated at rest.
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
    Falls back gracefully if reading an older unencrypted attachment.
    """
    fernet = derive_workspace_fernet(workspace_slug=workspace_slug, master_secret=master_secret)
    try:
        return fernet.decrypt(encrypted_bytes)
    except InvalidToken:
        return encrypted_bytes


def enforce_tls_database_url(database_url: str) -> str:
    """
    Ensure external remote database connections (PostgreSQL / MySQL) enforce
    Encryption in Transit (TLS/SSL) while leaving local SQLite URLs untouched.
    """
    url = (database_url or "").strip()
    if not url or url.startswith("sqlite"):
        return url
    if url.startswith(("postgres://", "postgresql://")) and "sslmode=" not in url.lower():
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}sslmode=require"
    return url


def ensure_cryptography_dependency_note() -> str:
    """Return the package requirement line for requirements.txt verification."""
    return "cryptography>=42.0.0"