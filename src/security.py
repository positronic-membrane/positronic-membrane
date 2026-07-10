import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from src import config

_CIPHERTEXT_PREFIX = "fernet:v1:"

# Fixed, application-specific salt for PBKDF2 domain separation. Not a secret
# in itself — JANUS_ENCRYPTION_KEY is expected to be a high-entropy value, so
# this salt's job is domain separation, not defeating rainbow tables.
_PBKDF2_SALT = b"janus-security-v1-fernet-salt"
_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256

# SHA256 hex digest of the pre-#105 hardcoded default encryption key string,
# precomputed offline so the literal string itself does not appear anywhere
# in src/ (see issue #105). Used only to decrypt rows that were encrypted
# before JANUS_ENCRYPTION_KEY was ever set on this install.
_LEGACY_DEFAULT_KEY_DIGEST_HEX = "2c18ff4f40dc25cf4075b178ba06c919e412d49ea003774c6a9f06e3e33f4f83"


@lru_cache(maxsize=1)
def _derive_fernet_key(secret: str) -> bytes:
    """PBKDF2-HMAC-SHA256-derives a Fernet-compatible urlsafe-base64 key from
    the raw JANUS_ENCRYPTION_KEY secret. Cached because PBKDF2 at this
    iteration count is deliberately slow and callers may encrypt/decrypt many
    keys per process lifetime."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_PBKDF2_SALT,
        iterations=_PBKDF2_ITERATIONS,
    )
    derived = kdf.derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


def _get_fernet() -> Fernet:
    secret = config.JANUS_ENCRYPTION_KEY
    if not secret:
        raise RuntimeError(
            "JANUS_ENCRYPTION_KEY is not set — cannot encrypt an API key. "
            "Set JANUS_ENCRYPTION_KEY or disable EXTERNAL_AGENTS_ENABLED."
        )
    return Fernet(_derive_fernet_key(secret))


def _legacy_xor_transform(data: bytes, key_digest: bytes) -> bytes:
    """Symmetric XOR transform — same operation for encrypt and decrypt.
    key_digest must be a 32-byte SHA256 digest."""
    return bytes(b ^ key_digest[i % len(key_digest)] for i, b in enumerate(data))


def _legacy_candidate_key_digests() -> list:
    """Candidate SHA256 digests a legacy row may have been XOR-encrypted
    under: the currently configured JANUS_ENCRYPTION_KEY (an install may
    already have had a real key set before this fix), and the old hardcoded
    default (installs that never set the env var at all). Tried in this
    order since the current key, if set, is the more likely/recent case."""
    digests = []
    secret = config.JANUS_ENCRYPTION_KEY
    if secret:
        digests.append(hashlib.sha256(secret.encode("utf-8")).digest())
    digests.append(bytes.fromhex(_LEGACY_DEFAULT_KEY_DIGEST_HEX))
    return digests


def _legacy_xor_decrypt(enc_key: str) -> str:
    """Best-effort decrypt of a legacy (pre-fernet:v1:) XOR-ciphertext string,
    trying every candidate legacy key digest in turn. Returns "" if none
    succeed — never raises. Private: only used by the one-time database
    migration and the decrypt fallback path below.

    XOR has no integrity check, so a wrong candidate digest can still
    occasionally produce bytes that happen to decode as UTF-8 (more likely
    for short ciphertext). Requiring the decoded result to be non-empty and
    printable (API keys are always printable) makes such false positives
    far less likely, though it cannot eliminate them entirely."""
    try:
        encrypted_bytes = base64.b64decode(enc_key.encode("utf-8"))
    except Exception:
        return ""
    for key_digest in _legacy_candidate_key_digests():
        try:
            decrypted_bytes = _legacy_xor_transform(encrypted_bytes, key_digest)
            candidate = decrypted_bytes.decode("utf-8")
        except Exception:
            continue
        if candidate and candidate.isprintable():
            return candidate
    return ""


def encrypt_api_key(key: str) -> str:
    """
    Encrypts an API key with Fernet, derived from JANUS_ENCRYPTION_KEY via
    PBKDF2. Raises RuntimeError if no key is configured — encrypting with no
    key is a caller/config bug, not an expected runtime condition.
    """
    if not key:
        return ""
    fernet = _get_fernet()
    token = fernet.encrypt(key.encode("utf-8"))
    return _CIPHERTEXT_PREFIX + token.decode("utf-8")


def decrypt_api_key(enc_key: str) -> str:
    """
    Decrypts an API key. Dispatches on the 'fernet:v1:' prefix: new-format
    ciphertext is Fernet-decrypted; unprefixed values are treated as legacy
    XOR ciphertext and decrypted via the best-effort fallback. Never raises —
    any failure (wrong key, corrupt data, unmigrated legacy row) returns "".
    """
    if not enc_key:
        return ""
    if enc_key.startswith(_CIPHERTEXT_PREFIX):
        token = enc_key[len(_CIPHERTEXT_PREFIX):]
        try:
            fernet = _get_fernet()
            return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception:
            return ""
    return _legacy_xor_decrypt(enc_key)
