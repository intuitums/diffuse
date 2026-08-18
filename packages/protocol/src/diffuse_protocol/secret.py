"""AES-GCM sealed secrets with a rotatable key-encryption key.

Wire format:
    diffuse-secret.v1.<key_id>.<base64url(nonce || ciphertext+tag)>

``key_id`` is the first eight hex characters of SHA-256(kek) so operators can
rotate by configuring a previous KEK without rewriting every row at once.
Associated authenticated data (AAD) binds a ciphertext to its purpose so a
sealed delivery key cannot be substituted as a dispatch capability wrap.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from collections.abc import Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "diffuse-secret.v1"
NONCE_BYTES = 12
KEY_BYTES = 32
KEY_ID_HEX_CHARS = 8


class SealedSecretError(ValueError):
    """A sealed secret is malformed, uses an unknown key, or fails authentication."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def key_id_for(kek: bytes) -> str:
    return hashlib.sha256(kek).hexdigest()[:KEY_ID_HEX_CHARS]


def decode_kek(value: str, *, name: str) -> bytes:
    try:
        raw = _b64url_decode(value.strip())
    except (ValueError, TypeError) as error:
        raise SealedSecretError(f"{name} must be base64url text") from error
    if len(raw) != KEY_BYTES:
        raise SealedSecretError(f"{name} must decode to exactly {KEY_BYTES} bytes")
    return raw


def load_keks_from_env(
    primary_variable: str,
    *,
    previous_variable: str | None = None,
) -> tuple[bytes, ...]:
    """Return the current KEK followed by any previous KEKs for dual-read."""

    primary = os.environ.get(primary_variable, "").strip()
    if not primary:
        raise SealedSecretError(f"{primary_variable} must be configured")
    keys = [decode_kek(primary, name=primary_variable)]
    if previous_variable:
        previous = os.environ.get(previous_variable, "").strip()
        if previous:
            previous_key = decode_kek(previous, name=previous_variable)
            if previous_key != keys[0]:
                keys.append(previous_key)
    return tuple(keys)


def is_sealed(value: str) -> bool:
    return value.startswith(f"{PREFIX}.")


def seal(plaintext: str | bytes, *, kek: bytes, aad: str) -> str:
    if len(kek) != KEY_BYTES:
        raise SealedSecretError(f"KEK must be exactly {KEY_BYTES} bytes")
    raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(kek).encrypt(nonce, raw, aad.encode("utf-8"))
    return f"{PREFIX}.{key_id_for(kek)}.{_b64url_encode(nonce + ciphertext)}"


def unseal(
    value: str,
    *,
    keks: Iterable[bytes],
    aad: str,
    allow_legacy_plaintext: bool = False,
) -> str:
    if not is_sealed(value):
        if allow_legacy_plaintext:
            return value
        raise SealedSecretError("value is not a sealed secret")
    parts = value.split(".")
    if len(parts) != 4 or f"{parts[0]}.{parts[1]}" != PREFIX:
        raise SealedSecretError("sealed secret is malformed")
    _, _, expected_key_id, payload = parts
    try:
        packed = _b64url_decode(payload)
    except (ValueError, TypeError) as error:
        raise SealedSecretError("sealed secret payload is malformed") from error
    if len(packed) <= NONCE_BYTES:
        raise SealedSecretError("sealed secret payload is truncated")
    nonce, ciphertext = packed[:NONCE_BYTES], packed[NONCE_BYTES:]
    aad_bytes = aad.encode("utf-8")
    for kek in keks:
        if key_id_for(kek) != expected_key_id:
            continue
        try:
            return AESGCM(kek).decrypt(nonce, ciphertext, aad_bytes).decode("utf-8")
        except Exception as error:
            raise SealedSecretError("sealed secret authentication failed") from error
    raise SealedSecretError("sealed secret key id is unknown")
