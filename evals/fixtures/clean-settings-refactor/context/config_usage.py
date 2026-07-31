"""The only consumers of the settings module."""

from __future__ import annotations

from config.settings import read_batch_size, read_timeout_seconds


def build_client(session_factory):
    """Both readers are called once, at startup, before any request is served."""
    return session_factory(
        timeout=read_timeout_seconds(),
        batch_size=read_batch_size(),
    )
