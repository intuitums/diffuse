from __future__ import annotations

import json
from argparse import Namespace

import httpx
import pytest

from service import relay_cli


def test_pair_prints_the_one_time_node_credential(monkeypatch, capsys):
    monkeypatch.setattr(
        relay_cli.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json={
                "nodeToken": "n" * 43,
                "nodeId": 7,
                "githubInstallationId": 12345,
            },
        ),
    )

    relay_cli._pair(
        Namespace(
            gateway="https://integrations.diffuse.example",
            code="p" * 43,
            name="office-node",
        )
    )

    output = json.loads(capsys.readouterr().out)
    assert output["gateway_url"] == "https://integrations.diffuse.example"
    assert output["node_token"] == "n" * 43
    assert output["github_installation_id"] == 12345


def test_pair_rejects_invalid_gateway_metadata(monkeypatch):
    monkeypatch.setattr(
        relay_cli.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json={
                "nodeToken": "not a token",
                "nodeId": 7,
                "githubInstallationId": 12345,
            },
        ),
    )

    with pytest.raises(RuntimeError, match="invalid pairing metadata"):
        relay_cli._pair(
            Namespace(
                gateway="https://integrations.diffuse.example",
                code="p" * 43,
                name="office-node",
            )
        )
