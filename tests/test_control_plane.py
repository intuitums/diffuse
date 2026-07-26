from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest

from service.control_plane import ControlPlaneSnapshot, publish_snapshot


def _snapshot() -> ControlPlaneSnapshot:
    return ControlPlaneSnapshot.model_validate(
        {
            "deployment": {
                "deploymentKey": "local",
                "name": "Local",
                "kind": "self_hosted",
                "status": "healthy",
                "version": "test",
                "updatedAt": 1,
            },
            "repositories": [],
            "reviews": [],
        }
    )


def test_publish_snapshot_signs_exact_body_without_source_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = b"x" * 32
    secret_file = tmp_path / "signing-key"
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)
    captured = {}

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("service.control_plane.time.time", lambda: 1234)
    monkeypatch.setattr("service.control_plane.urllib.request.urlopen", fake_urlopen)

    publish_snapshot(
        _snapshot(),
        url="https://control.example",
        secret_file=secret_file,
    )

    request = captured["request"]
    expected = hmac.new(
        secret,
        b"1234." + request.data,
        hashlib.sha256,
    ).hexdigest()
    assert request.full_url == "https://control.example/v1/data-plane/snapshot"
    assert request.headers["X-diffuse-timestamp"] == "1234"
    assert request.headers["X-diffuse-signature"] == f"sha256={expected}"
    assert b"source" not in request.data
    assert captured["timeout"] == 10


def test_publish_snapshot_rejects_readable_secret_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "signing-key"
    secret_file.write_bytes(b"x" * 32)
    secret_file.chmod(0o644)

    with pytest.raises(ValueError, match="group/world"):
        publish_snapshot(
            _snapshot(),
            url="https://control.example",
            secret_file=secret_file,
        )
