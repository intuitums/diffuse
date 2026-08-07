"""Control-plane configuration for signed agent-session capabilities.

The runner deliberately never receives this key: it presents an opaque bearer
capability to the control plane, which verifies the signature before serving a
tool.  Keeping the signing key separate from ``DIFFUSE_API_TOKEN`` limits the
blast radius of either credential and lets an operator rotate agent sessions
without rotating their recovery API credential.
"""

from __future__ import annotations

import os

CAPABILITY_SIGNING_KEY_VARIABLE = "DIFFUSE_AGENT_CAPABILITY_SIGNING_KEY"
MIN_CAPABILITY_SIGNING_KEY_BYTES = 32


def session_capability_signing_key() -> bytes:
    """Return the configured HMAC key, refusing weak or missing configuration."""

    value = os.environ.get(CAPABILITY_SIGNING_KEY_VARIABLE, "")
    key = value.encode("utf-8")
    if len(key) < MIN_CAPABILITY_SIGNING_KEY_BYTES:
        raise ValueError(
            f"{CAPABILITY_SIGNING_KEY_VARIABLE} must be at least "
            f"{MIN_CAPABILITY_SIGNING_KEY_BYTES} bytes"
        )
    return key


def probe_session_capability_signing_key() -> None:
    """Startup probe: absent is fine until agent tools are enabled; weak is not.

    When the variable is unset the internal agent-tool listener stays down. When
    it is set, refuse to boot with a short key rather than minting forgeable
    capabilities after the process is already serving traffic.
    """

    if not os.environ.get(CAPABILITY_SIGNING_KEY_VARIABLE, "").strip():
        return
    session_capability_signing_key()
