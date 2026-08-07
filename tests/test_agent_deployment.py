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


def test_agent_runner_image_is_not_the_control_plane_image():
    """Gate B: pinned CLIs live in a dedicated image without Diffuse secrets."""

    dockerfile = (REPOSITORY_ROOT / "Dockerfile.agent-runner").read_text()
    assert "@anthropic-ai/claude-code@" in dockerfile
    assert "@openai/codex@" in dockerfile
    assert "uid 10001" in dockerfile or "--uid 10001" in dockerfile
    assert "DATABASE_URL=" in dockerfile
    assert "GITHUB_APP_PRIVATE_KEY=" in dockerfile
    # Must not bake the control-plane frozen binary or LiteLLM collect step.
    assert "pyinstaller" not in dockerfile
    assert "collect-data litellm" not in dockerfile
    assert (REPOSITORY_ROOT / "deploy" / "agent-runner" / "docker-entrypoint.sh").is_file()


def test_agent_runner_service_has_no_control_plane_secrets_or_env_file():
    for path in COMPOSE_FILES:
        text = path.read_text()
        runner = _service_block(text, "agent-runner")
        assert 'profiles: ["agent"]' in runner
        assert "env_file:" not in runner
        assert "DATABASE_URL" not in runner
        assert "GITHUB_" not in runner
        assert "REVIEW_MODEL" not in runner
        assert "networks: [agent_mcp, agent_egress]" in runner
        assert "user: \"10001:10001\"" in runner
        # Transitional: credential volume still sole-written by worker.
        assert "agent_data:" not in runner


def test_agent_runner_image_does_not_make_the_opt_in_profile_required():
    """Compose interpolates disabled-profile services before filtering them."""

    release_compose = _service_block(
        (REPOSITORY_ROOT / "deploy" / "compose.yaml").read_text(), "agent-runner"
    )
    assert "${DIFFUSE_AGENT_RUNNER_IMAGE:?" not in release_compose
    assert "${DIFFUSE_AGENT_RUNNER_IMAGE:-diffuse-agent-runner:local}" in release_compose
