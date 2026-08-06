"""The agent credential volume is a runtime security boundary, not YAML garnish."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = (
    REPOSITORY_ROOT / "docker-compose.yml",
    REPOSITORY_ROOT / "deploy" / "compose.yaml",
)


def _worker_block(text: str) -> str:
    start = text.index("  worker:\n")
    return text[start : text.index("\nvolumes:\n", start)]


def test_agent_volume_has_exactly_one_writer_in_both_compose_profiles():
    """Two vendor refreshes against one OAuth token can sign each other out."""

    for path in COMPOSE_FILES:
        text = path.read_text()
        worker = _worker_block(text)
        assert "agent_data:/var/lib/diffuse/agent" in worker
        assert "DIFFUSE_AGENT_HOME: /var/lib/diffuse/agent" in worker
        assert "HOME: /var/lib/diffuse/agent/home" in worker
        assert text.count("agent_data:/var/lib/diffuse/agent") == 1
        assert "  agent_data:\n" in text


def test_runtime_image_pins_the_volume_owner_and_mode():
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()
    assert "addgroup --system --gid 10001 diffuse" in dockerfile
    assert "adduser --system --uid 10001 --ingroup diffuse" in dockerfile
    assert "--mode=700 /var/lib/diffuse/agent" in dockerfile
    assert "'10001:10001:700'" in dockerfile
