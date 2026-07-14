"""Tests for the still-image (alarm snapshot) decryptor."""

from __future__ import annotations

from hashlib import md5

import pytest
from Crypto.Cipher import AES

from custom_components.ezviz_stream.const import HIK_ENCRYPTION_HEADER
from custom_components.ezviz_stream.decrypt_image import (
    StillImageDecryptError,
    decrypt_still_image,
)

_CODE = "ABCDEF"
_IV = b"01234567" + bytes(8)


def _key(code: str) -> bytes:
    return code.ljust(16, "\x00")[:16].encode()


def _password_hash(code: str) -> bytes:
    inner = md5(code.encode()).hexdigest()  # noqa: S324 - matches the on-wire scheme
    return md5(inner.encode()).hexdigest().encode()  # noqa: S324


def _encrypt(plain: bytes, code: str) -> bytes:
    """Build one `hikencodepicture` block: header + password hash + AES-CBC cipher."""
    pad = 16 - (len(plain) % 16)  # PKCS#7, always 1..16
    padded = plain + bytes([pad]) * pad
    ciphertext = AES.new(_key(code), AES.MODE_CBC, _IV).encrypt(padded)
    return HIK_ENCRYPTION_HEADER + _password_hash(code) + ciphertext


def test_roundtrip() -> None:
    """A block encrypted with the code decrypts back to the original bytes."""
    plain = b"\xff\xd8\xff\xe0 pretend-jpeg payload of arbitrary length \x00\x01\x02"
    assert decrypt_still_image(_encrypt(plain, _CODE), _CODE) == plain


def test_plaintext_passthrough() -> None:
    """An image without the marker is returned unchanged (not all cams encrypt)."""
    plain = b"\xff\xd8\xff\xe0 an ordinary jpeg with no hik header"
    assert decrypt_still_image(plain, _CODE) == plain


def test_preamble_before_marker_is_trimmed() -> None:
    """Any bytes before the first marker are discarded before decrypting."""
    plain = b"the real image bytes"
    blob = b"junk-preamble" + _encrypt(plain, _CODE)
    assert decrypt_still_image(blob, _CODE) == plain


def test_concatenated_blocks_are_joined() -> None:
    """Several concatenated encrypted blocks decrypt and join in order."""
    a, b = b"first-block-data", b"second-block-data!!"
    blob = _encrypt(a, _CODE) + _encrypt(b, _CODE)
    assert decrypt_still_image(blob, _CODE) == a + b


def test_wrong_code_raises() -> None:
    """A wrong verification code fails the password-hash check."""
    blob = _encrypt(b"secret frame", _CODE)
    with pytest.raises(StillImageDecryptError):
        decrypt_still_image(blob, "WRONG1")
