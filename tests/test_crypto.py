"""Tests for the optional encryption codec."""

from __future__ import annotations

import pytest

from clipsync.crypto import (
    CryptoError,
    FernetCodec,
    derive_key,
    new_salt,
)


def test_roundtrip() -> None:
    codec = FernetCodec.generate()
    plaintext = b'{"hello": "world"}'
    ciphertext = codec.encrypt(plaintext)
    assert ciphertext != plaintext
    assert codec.decrypt(ciphertext) == plaintext


def test_tamper_detected() -> None:
    codec = FernetCodec.generate()
    token = bytearray(codec.encrypt(b"secret"))
    token[-1] ^= 0x01
    with pytest.raises(CryptoError):
        codec.decrypt(bytes(token))


def test_wrong_key_detected() -> None:
    a = FernetCodec.generate()
    b = FernetCodec.generate()
    token = a.encrypt(b"secret")
    with pytest.raises(CryptoError):
        b.decrypt(token)


def test_malformed_token_detected() -> None:
    codec = FernetCodec.generate()
    with pytest.raises(CryptoError):
        codec.decrypt(b"not a fernet token")


def test_invalid_key_rejected() -> None:
    with pytest.raises(CryptoError):
        FernetCodec(b"too-short")


def test_passphrase_derivation_is_deterministic_per_salt() -> None:
    salt = b"0123456789abcdef"
    k1 = derive_key("correct horse battery staple", salt)
    k2 = derive_key("correct horse battery staple", salt)
    assert k1 == k2
    assert derive_key("correct horse battery staple", b"fedcba9876543210") != k1
    # Derived key is usable as a Fernet key.
    codec = FernetCodec(k1)
    assert codec.decrypt(codec.encrypt(b"x")) == b"x"


def test_from_passphrase_interops() -> None:
    salt = new_salt()
    a = FernetCodec.from_passphrase("pw", salt)
    b = FernetCodec.from_passphrase("pw", salt)
    assert b.decrypt(a.encrypt(b"msg")) == b"msg"
    c = FernetCodec.from_passphrase("other", salt)
    with pytest.raises(CryptoError):
        c.decrypt(a.encrypt(b"msg"))


def test_new_salt() -> None:
    assert len(new_salt()) == 16
    assert new_salt() != new_salt()
    with pytest.raises(ValueError):
        new_salt(4)


def test_empty_passphrase_rejected() -> None:
    with pytest.raises(ValueError):
        derive_key("", b"0123456789abcdef")
