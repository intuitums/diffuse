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


def test_agent_volume_has_exactly_one_writer_on_the_runner_skeleton():
    """Vendor refreshes against one OAuth token must not race the worker."""

    for path in COMPOSE_FILES:
        text = path.read_text()
        runner = _service_block(text, "agent-runner")
        assert "agent_data:/var/lib/diffuse/agent" in runner
        assert "DIFFUSE_AGENT_HOME: /var/lib/diffuse/agent" in runner
        assert "HOME: /var/lib/diffuse/agent/home" in runner
        assert "HTTP_PROXY: http://egress-proxy:3128" in runner
        assert "HTTPS_PROXY: http://egress-proxy:3128" in runner
        assert "egress-proxy:" in runner
        assert "condition: service_healthy" in runner
        assert "profiles: [\"agent\"]" in runner
        assert "env_file:" not in runner
        assert "DATABASE_URL" not in runner
        assert text.count("agent_data:/var/lib/diffuse/agent") == 1
        assert "  agent_data:\n" in text


def test_worker_and_app_never_mount_the_credential_volume():
    """Target architecture: only the isolated agent-runner holds CLI credentials."""

    for path in COMPOSE_FILES:
        text = path.read_text()
        assert "agent_data" not in _service_block(text, "worker")
        assert "DIFFUSE_AGENT_HOME" not in _service_block(text, "worker")
        assert "agent_data" not in _service_block(text, "app")


def test_runtime_image_pins_the_volume_owner_and_mode():
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()
    assert "addgroup --system --gid 10001 diffuse" in dockerfile
    assert "adduser --system --uid 10001 --ingroup diffuse" in dockerfile
    assert "--mode=700 /var/lib/diffuse/agent" in dockerfile
    assert "'10001:10001:700'" in dockerfile


def test_runtime_image_creates_the_home_compose_points_at():
    """`agent_login_home()` creates this lazily, but HOME is set service-wide.

    Without it in the image, a stack that has never run an agent login boots its
    agent-runner with HOME pointing at a directory that does not exist.
    """

    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()
    assert "--mode=700 /var/lib/diffuse/agent/home" in dockerfile

    for path in COMPOSE_FILES:
        runner = _service_block(path.read_text(), "agent-runner")
        assert "HOME: /var/lib/diffuse/agent/home" in runner
