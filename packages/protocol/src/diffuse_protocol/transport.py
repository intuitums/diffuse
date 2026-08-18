"""Shared AES-GCM key that wraps capabilities inside Agent Dispatch envelopes."""

from __future__ import annotations

from diffuse_protocol.secret import (
    SealedSecretError,
    load_keks_from_env,
    seal,
    unseal,
)

TRANSPORT_SECRET_VARIABLE = "DIFFUSE_REVIEW_AGENT_TRANSPORT_SECRET"
TRANSPORT_SECRET_PREVIOUS_VARIABLE = "DIFFUSE_REVIEW_AGENT_TRANSPORT_SECRET_PREVIOUS"
CAPABILITY_AAD = "review_agent.dispatch.capability"


def transport_keks() -> tuple[bytes, ...]:
    try:
        return load_keks_from_env(
            TRANSPORT_SECRET_VARIABLE,
            previous_variable=TRANSPORT_SECRET_PREVIOUS_VARIABLE,
        )
    except SealedSecretError as error:
        raise ValueError(str(error)) from error


def validate_transport_secret() -> None:
    transport_keks()


def wrap_capability(capability: str) -> str:
    return seal(capability, kek=transport_keks()[0], aad=CAPABILITY_AAD)


def unwrap_capability(value: str) -> str:
    try:
        return unseal(value, keks=transport_keks(), aad=CAPABILITY_AAD)
    except SealedSecretError as error:
        raise ValueError("dispatch capability wrap is invalid") from error
