"""Optional end-to-end encryption for clip frames.

The engine treats transports as opaque byte pipes.  Wrapping the plaintext canonical
JSON frame in :class:`FernetCodec` gives authenticated encryption (AES-128-CBC + HMAC
inside Fernet): the relay sees ciphertext only, and tampering or a wrong key raises
:class:`CryptoError` instead of yielding data.

Keys are never derived from a low-entropy secret without a KDF: use
:func:`derive_key` with a random salt when starting from a passphrase.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken

__all__ = [
    "Codec",
    "CryptoError",
    "FernetCodec",
    "derive_key",
    "new_salt",
]

_SCRYPT_N = 2**14


class CryptoError(Exception):
    """Decryption failed (wrong key, tampered ciphertext, malformed token)."""


@runtime_checkable
class Codec(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> bytes: ...


class FernetCodec:
    """Authenticated symmetric encryption using ``cryptography``'s Fernet.

    The same codec instance is used by both peers (shared key).  Frames are opaque
    bytes; a transport never needs to know whether a frame is encrypted.
    """

    def __init__(self, key: bytes | str) -> None:
        if isinstance(key, str):
            key = key.encode("ascii")
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:  # malformed key
            raise CryptoError(f"invalid Fernet key: {exc}") from exc
        self._key = key

    @classmethod
    def generate(cls) -> "FernetCodec":
        return cls(Fernet.generate_key())

    @classmethod
    def from_passphrase(cls, passphrase: str, salt: bytes) -> "FernetCodec":
        return cls(derive_key(passphrase, salt))

    @property
    def key(self) -> bytes:
        """The urlsafe-base64 key (share out-of-band with the phone; never commit)."""
        return self._key

    def encrypt(self, plaintext: bytes) -> bytes:
        if not isinstance(plaintext, (bytes, bytearray)):
            raise TypeError("plaintext must be bytes")
        return self._fernet.encrypt(bytes(plaintext))

    def decrypt(self, ciphertext: bytes) -> bytes:
        try:
            return self._fernet.decrypt(bytes(ciphertext))
        except (InvalidToken, ValueError, TypeError) as exc:
            raise CryptoError("could not decrypt frame (wrong key or tampered data)") from exc


def new_salt(size: int = 16) -> bytes:
    """Random salt for :func:`derive_key`; store it next to (not with) the key."""
    import os

    if size < 8:
        raise ValueError("salt must be at least 8 bytes")
    return os.urandom(size)


def derive_key(
    passphrase: str,
    salt: bytes,
    *,
    n: int = _SCRYPT_N,
    r: int = 8,
    p: int = 1,
) -> bytes:
    """Derive a Fernet key from a passphrase with scrypt.

    ``n`` defaults to 2**14 (cheap enough for a laptop/phone pairing step).  The salt
    must be random and unique per pairing; both sides need the same passphrase and salt.
    """
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    if len(salt) < 8:
        raise ValueError("salt must be at least 8 bytes")
    raw = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return base64.urlsafe_b64encode(raw)
