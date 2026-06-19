import os
import base64
import hashlib

def _get_encryption_key() -> bytes:
    """Gets key bytes from environment or a standard default fallback."""
    secret = os.getenv("JANUS_ENCRYPTION_KEY", "default-janus-secret-key-321-shift")
    return hashlib.sha256(secret.encode("utf-8")).digest()

def encrypt_api_key(key: str) -> str:
    """
    Encrypts an API key using standard base64 encoded XOR against a SHA256 hashed secret.
    """
    if not key:
        return ""
    key_bytes = key.encode("utf-8")
    cipher_bytes = _get_encryption_key()
    
    # Simple XOR byte-wise cycle
    encrypted_bytes = bytearray(
        b ^ cipher_bytes[i % len(cipher_bytes)] for i, b in enumerate(key_bytes)
    )
    return base64.b64encode(encrypted_bytes).decode("utf-8")

def decrypt_api_key(enc_key: str) -> str:
    """
    Decrypts an API key using standard base64 decoded XOR against a SHA256 hashed secret.
    """
    if not enc_key:
        return ""
    try:
        encrypted_bytes = base64.b64decode(enc_key.encode("utf-8"))
        cipher_bytes = _get_encryption_key()
        
        decrypted_bytes = bytearray(
            b ^ cipher_bytes[i % len(cipher_bytes)] for i, b in enumerate(encrypted_bytes)
        )
        return decrypted_bytes.decode("utf-8")
    except Exception:
        return ""
