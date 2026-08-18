"""Rotatable AES-GCM sealed secrets stay confidential and purpose-bound."""

from __future__ import annotations

import pytest
from diffuse_protocol.secret import (
    SealedSecretError,
    is_sealed,
    key_id_for,
    seal,
    unseal,
)


def _kek(seed: int = 1) -> bytes:
    return bytes((seed + i) % 256 for i in range(32))


def test_seal_round_trip_and_rejects_wrong_aad_or_key():
    kek = _kek()
    sealed = seal("delivery-secret", kek=kek, aad="purpose.a")

    assert is_sealed(sealed)
    assert "delivery-secret" not in sealed
    assert unseal(sealed, keks=(kek,), aad="purpose.a") == "delivery-secret"

    with pytest.raises(SealedSecretError):
        unseal(sealed, keks=(kek,), aad="purpose.b")
    with pytest.raises(SealedSecretError):
        unseal(sealed, keks=(_kek(2),), aad="purpose.a")


def test_unseal_accepts_previous_kek_during_rotation():
    old = _kek(1)
    new = _kek(2)
    sealed = seal("rotated", kek=old, aad="cred")

    assert key_id_for(old) in sealed
    assert unseal(sealed, keks=(new, old), aad="cred") == "rotated"


def test_legacy_plaintext_dual_read_is_opt_in():
    with pytest.raises(SealedSecretError):
        unseal("plaintext", keks=(_kek(),), aad="cred")

    assert (
        unseal("plaintext", keks=(_kek(),), aad="cred", allow_legacy_plaintext=True)
        == "plaintext"
    )


def test_relay_and_protocol_sealed_secret_modules_stay_wire_compatible():
    from diffuse_relay import sealed_secret as relay_sealed

    kek = _kek(9)
    service_sealed = seal("shared", kek=kek, aad="event")
    hosted = relay_sealed.seal("shared", kek=kek, aad="event")

    assert relay_sealed.unseal(service_sealed, keks=(kek,), aad="event") == "shared"
    assert unseal(hosted, keks=(kek,), aad="event") == "shared"
