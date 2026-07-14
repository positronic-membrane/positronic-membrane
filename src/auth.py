import logging
import os
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import JWTError, jwt

from src.config import ROOT_DIR

logger = logging.getLogger("JanusAuth")

KEYS_DIR = ROOT_DIR / ".keys"
PRIVATE_KEY_PATH = KEYS_DIR / "jwt_private.pem"
PUBLIC_KEY_PATH = KEYS_DIR / "jwt_public.pem"

# Cache loaded key contents
_private_key_pem = None
_public_key_pem = None

def _generate_key_pair():
    """Generates an RSA 2048 key pair and stores them locally."""
    logger.info("Generating new RS256 key pair for JWT signing...")
    KEYS_DIR.mkdir(parents=True, exist_ok=True)

    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )

    # Serialize private key to PEM
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    # Serialize public key to PEM
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    with open(PRIVATE_KEY_PATH, "wb") as f:
        f.write(private_pem)
    with open(PUBLIC_KEY_PATH, "wb") as f:
        f.write(public_pem)

    logger.info(f"Keys successfully saved in {KEYS_DIR}")

def keys_available() -> bool:
    """True if _load_keys() would succeed without needing to auto-generate —
    i.e. either the env var pair or both local PEM files are already present."""
    if os.getenv("JWT_PRIVATE_KEY") and os.getenv("JWT_PUBLIC_KEY"):
        return True
    return PRIVATE_KEY_PATH.exists() and PUBLIC_KEY_PATH.exists()


def _load_keys():
    """Loads RSA private/public keys from environment or local files."""
    global _private_key_pem, _public_key_pem

    if _private_key_pem and _public_key_pem:
        return _private_key_pem, _public_key_pem

    # Check if keys are provided via environment variables (preferred for cloud hosting)
    env_private = os.getenv("JWT_PRIVATE_KEY")
    env_public = os.getenv("JWT_PUBLIC_KEY")

    if env_private and env_public:
        _private_key_pem = env_private.replace("\\n", "\n").encode("utf-8")
        _public_key_pem = env_public.replace("\\n", "\n").encode("utf-8")
        logger.info("Loaded RS256 keys from environment variables.")
        return _private_key_pem, _public_key_pem

    # Fallback to local files
    if not PRIVATE_KEY_PATH.exists() or not PUBLIC_KEY_PATH.exists():
        _generate_key_pair()

    with open(PRIVATE_KEY_PATH, "rb") as f:
        _private_key_pem = f.read()
    with open(PUBLIC_KEY_PATH, "rb") as f:
        _public_key_pem = f.read()

    logger.info("Loaded RS256 keys from local .keys files.")
    return _private_key_pem, _public_key_pem

def create_access_token(party_id: str, role: str, expires_delta: timedelta = None) -> str:
    """Creates a JWT access token signed with RS256 private key."""
    private_key, _ = _load_keys()

    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(days=1)  # 24 hours default

    to_encode = {
        "sub": party_id,
        "role": role,
        "exp": expire
    }

    encoded_jwt = jwt.encode(to_encode, private_key, algorithm="RS256")
    return encoded_jwt

def decode_access_token(token: str) -> dict:
    """Decodes and validates a JWT access token using RS256 public key. Returns claims dict."""
    _, public_key = _load_keys()
    try:
        payload = jwt.decode(token, public_key, algorithms=["RS256"])
        return payload
    except JWTError as e:
        logger.warning(f"JWT verification failed: {e}")
        raise ValueError(f"Invalid token: {e}") from e
