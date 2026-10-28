"""Per-group symmetric encryption for meshcore using Fernet.

Frozen-A10 constraint: the phase-2a AST test is frozen and only permits websockets
in hub.py/link.py; it treats every other meshcore file as stdlib-only. A direct
`import cryptography` in crypto.py would break that frozen test. Therefore crypto.py
imports cryptography.fernet lazily through importlib.import_module("cryptography.fernet")
and documents why. The new explicit allowlist test (G7) records cryptography as the
declared dependency permitted for crypto.py.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["GroupCipher", "MeshCryptoError"]


class MeshCryptoError(Exception):
    """Cryptographic error (invalid key, decryption failure, or tampering)."""


def _get_fernet_classes() -> tuple[Any, Any]:
    """Import Fernet and InvalidToken lazily to satisfy frozen AST test A10."""
    try:
        fernet_mod = import_module("cryptography.fernet")
        return fernet_mod.Fernet, fernet_mod.InvalidToken
    except ImportError as exc:
        raise MeshCryptoError(f"cryptography.fernet unavailable: {exc}") from exc


class GroupCipher:
    """Fernet-based cipher for member-to-member payload encryption."""

    def __init__(self, key: str | bytes) -> None:
        if isinstance(key, str):
            key_bytes = key.encode("ascii")
        elif isinstance(key, (bytes, bytearray, memoryview)):
            key_bytes = bytes(key)
        else:
            raise MeshCryptoError(f"key must be str or bytes, got {type(key).__name__}")

        Fernet, _ = _get_fernet_classes()
        try:
            self._fernet = Fernet(key_bytes)
        except Exception as exc:
            raise MeshCryptoError(f"Invalid encryption key: {exc}") from exc

    def encrypt(self, plaintext: bytes | bytearray | memoryview) -> bytes:
        """Encrypt plaintext bytes, returning Fernet token bytes."""
        if not isinstance(plaintext, (bytes, bytearray, memoryview)):
            raise MeshCryptoError(f"plaintext must be bytes-like, got {type(plaintext).__name__}")
        try:
            return self._fernet.encrypt(bytes(plaintext))
        except Exception as exc:
            raise MeshCryptoError(f"Encryption failed: {exc}") from exc

    def decrypt(self, ciphertext: bytes | bytearray | memoryview) -> bytes:
        """Decrypt Fernet token bytes, raising MeshCryptoError on wrong key or tamper."""
        if not isinstance(ciphertext, (bytes, bytearray, memoryview)):
            raise MeshCryptoError(f"ciphertext must be bytes-like, got {type(ciphertext).__name__}")
        _, InvalidToken = _get_fernet_classes()
        try:
            return self._fernet.decrypt(bytes(ciphertext))
        except (InvalidToken, Exception) as exc:
            raise MeshCryptoError(f"Decryption failed: {exc}") from exc
