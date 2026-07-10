import base64
import hashlib
from pathlib import Path

import pytest

import src.config as config
from src import security
from src.security import decrypt_api_key, encrypt_api_key


@pytest.fixture(autouse=True)
def _clear_fernet_key_cache():
    """_derive_fernet_key is an lru_cache — clear it around every test so a
    monkeypatched JANUS_ENCRYPTION_KEY from one test can't leak into another
    via a cached derived key for the same secret string."""
    security._derive_fernet_key.cache_clear()
    yield
    security._derive_fernet_key.cache_clear()


def test_roundtrip_with_key_set(monkeypatch):
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "test-secret-abc123")
    enc = encrypt_api_key("sk-proj-12345")
    assert enc != "sk-proj-12345"
    assert decrypt_api_key(enc) == "sk-proj-12345"


def test_encrypt_uses_fernet_v1_prefix(monkeypatch):
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "test-secret-abc123")
    assert encrypt_api_key("x").startswith("fernet:v1:")


def test_encrypt_empty_or_none_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "test-secret-abc123")
    assert encrypt_api_key("") == ""
    assert encrypt_api_key(None) == ""


def test_decrypt_empty_string_returns_empty():
    assert decrypt_api_key("") == ""


def test_encrypt_raises_without_key(monkeypatch):
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "")
    with pytest.raises(RuntimeError):
        encrypt_api_key("some-key")


def test_decrypt_wrong_key_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "key-a")
    enc = encrypt_api_key("sk-proj-12345")
    security._derive_fernet_key.cache_clear()
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "key-b")
    assert decrypt_api_key(enc) == ""


def test_decrypt_garbage_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "test-secret-abc123")
    assert decrypt_api_key("not-valid-base64-!!!") == ""
    assert decrypt_api_key("fernet:v1:garbage") == ""


def test_decrypt_legacy_xor_ciphertext_with_env_key(monkeypatch):
    secret = "test-secret-abc123"
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", secret)
    plaintext = "sk-old-key"
    key_digest = hashlib.sha256(secret.encode("utf-8")).digest()
    xor_bytes = bytes(
        b ^ key_digest[i % len(key_digest)] for i, b in enumerate(plaintext.encode("utf-8"))
    )
    legacy_ciphertext = base64.b64encode(xor_bytes).decode("utf-8")
    assert decrypt_api_key(legacy_ciphertext) == plaintext


def test_decrypt_legacy_xor_ciphertext_with_old_default_digest(monkeypatch):
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "")
    plaintext = "sk-old-default-key"
    key_digest = bytes.fromhex(security._LEGACY_DEFAULT_KEY_DIGEST_HEX)
    xor_bytes = bytes(
        b ^ key_digest[i % len(key_digest)] for i, b in enumerate(plaintext.encode("utf-8"))
    )
    legacy_ciphertext = base64.b64encode(xor_bytes).decode("utf-8")
    assert decrypt_api_key(legacy_ciphertext) == plaintext


def test_fernet_key_derivation_is_deterministic():
    key_a = security._derive_fernet_key("same-secret")
    key_b = security._derive_fernet_key("same-secret")
    assert key_a == key_b


def test_legacy_default_key_literal_string_absent_from_src():
    """The old hardcoded default encryption key string must never appear in
    source (only its precomputed SHA256 digest may exist, in security.py)."""
    src_dir = Path(__file__).resolve().parent.parent / "src"
    banned = "default-janus-secret-key-321-shift"
    offenders = []
    for path in src_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if banned in text:
            offenders.append(str(path))
    assert offenders == [], f"Legacy default key literal found in: {offenders}"
