from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

_OPENSSL_PREFIX = b"Salted__"
_SIGNATURE_PREFIX = "Web@Rj$2020!"


def encrypt_eweb_password(password: str, passphrase: str, *, salt: bytes | None = None) -> str:
    """Create the OpenSSL-compatible AES value used by the eWeb login page."""
    salt = salt or os.urandom(8)
    if len(salt) != 8:
        raise ValueError("eWeb AES salt must contain 8 bytes")

    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt)
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    encrypted = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))
    return base64.b64encode(_OPENSSL_PREFIX + salt + encrypted).decode("ascii")


def compact_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8")


def signed_headers(payload_bytes: bytes) -> dict[str, str]:
    byte_length = str(len(payload_bytes))
    payload_text = payload_bytes.decode("utf-8")
    return {
        "Content-Type": "application/json",
        "Content-Accept": _md5(_SIGNATURE_PREFIX + byte_length),
        "Contents-Accept": _md5(_SIGNATURE_PREFIX + payload_text),
    }


def _evp_bytes_to_key(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    material = b""
    previous = b""
    while len(material) < 48:
        previous = hashlib.md5(previous + passphrase + salt).digest()
        material += previous
    return material[:32], material[32:48]


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()
