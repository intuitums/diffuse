"""The source and release environment examples describe Agent-only setup."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_environment_examples_select_an_agent_and_document_its_grants():
    for path in (ROOT / ".env.example", ROOT / "deploy" / "env.example"):
        text = path.read_text()
        assert "REVIEW_AGENT=codex" in text
        assert "DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY=" in text
        assert "DIFFUSE_REVIEW_AGENT_CAPABILITY_SIGNING_KEY=" in text
        assert "DIFFUSE_REVIEW_AGENT_TRANSPORT_SECRET=" in text
        assert "REVIEW_MODEL" not in text
    example = (ROOT / ".env.example").read_text()
    assert "base64url-encoded 32-byte raw Ed25519" in example
    assert "openssl rand -hex 32" in example
    assert "do not reuse either value" not in example


def test_release_bundle_ships_the_customer_environment_example():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "deploy/env.example" in workflow
