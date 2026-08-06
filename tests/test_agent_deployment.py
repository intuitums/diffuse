"""The agent credential volume is a runtime security boundary, not YAML garnish."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = (
    REPOSITORY_ROOT / "docker-compose.yml",
    REPOSITORY_ROOT / "deploy" / "compose.yaml",
)


def _service_block(text: str, name: str) -> str:
    """Slice one service out of a Compose file by its own indentation.

    Not "from this service to the top-level `volumes:`" -- that only isolates
    the worker while the worker happens to be the last service in the file, and
    would silently widen to cover whatever service gets added after it.
    """

    lines = text.splitlines()
    start = lines.index(f"  {name}:")
    body = []
    for line in lines[start + 1 :]:
        starts_a_sibling = line and not line.startswith("   ")
        if starts_a_sibling and line.strip():
            break
        body.append(line)
    return "\n".join(body)


def test_agent_volume_has_exactly_one_writer_in_both_compose_profiles():
    """Two vendor refreshes against one OAuth token can sign each other out."""

    for path in COMPOSE_FILES:
        text = path.read_text()
        worker = _service_block(text, "worker")
        assert "agent_data:/var/lib/diffuse/agent" in worker
        assert "DIFFUSE_AGENT_HOME: /var/lib/diffuse/agent" in worker
        assert "HOME: /var/lib/diffuse/agent/home" in worker
        assert text.count("agent_data:/var/lib/diffuse/agent") == 1
        assert "  agent_data:\n" in text


def test_the_app_service_never_mounts_the_credential_volume():
    """Stated positively, so the count above cannot pass by mounting it on app."""

    for path in COMPOSE_FILES:
        assert "agent_data" not in _service_block(path.read_text(), "app")


def test_runtime_image_pins_the_volume_owner_and_mode():
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()
    assert "addgroup --system --gid 10001 diffuse" in dockerfile
    assert "adduser --system --uid 10001 --ingroup diffuse" in dockerfile
    assert "--mode=700 /var/lib/diffuse/agent" in dockerfile
    assert "'10001:10001:700'" in dockerfile


def test_runtime_image_creates_the_home_compose_points_at():
    """`agent_login_home()` creates this lazily, but HOME is set service-wide.

    Without it in the image, a stack that has never run an agent login boots its
    worker with HOME pointing at a directory that does not exist.
    """

    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()
    assert "--mode=700 /var/lib/diffuse/agent/home" in dockerfile

    for path in COMPOSE_FILES:
        worker = _service_block(path.read_text(), "worker")
        assert "HOME: /var/lib/diffuse/agent/home" in worker
