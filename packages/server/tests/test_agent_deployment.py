"""Compose declarations are part of the native-runner security boundary."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = (
    REPOSITORY_ROOT / "compose.yaml",
    REPOSITORY_ROOT / "deploy" / "compose.yaml",
)


def _service_block(text: str, name: str) -> str:
    lines = text.splitlines()
    start = lines.index(f"  {name}:")
    body = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        body.append(line)
    return "\n".join(body)


def test_each_runner_has_its_own_pinned_image_credential_home_and_egress_proxy():
    for path in COMPOSE_FILES:
        text = path.read_text()
        for runtime in ("claude", "codex"):
            runner = _service_block(text, f"agent-host-{runtime}")
            assert f"DIFFUSE_{runtime.upper()}_RUNNER_IMAGE" in runner
            assert f"{runtime}_agent_data:/var/lib/diffuse/agent" in runner
            assert "DIFFUSE_REVIEW_AGENT_HOME: /var/lib/diffuse/agent" in runner
            assert "HOME: /var/lib/diffuse/agent/home" in runner
            assert f"HTTP_PROXY: http://egress-proxy-{runtime}:3128" in runner
            assert f"HTTPS_PROXY: http://egress-proxy-{runtime}:3128" in runner
            assert f"DIFFUSE_REVIEW_AGENT_EGRESS_PROXY_HOST: egress-proxy-{runtime}" in runner
            assert f"egress-proxy-{runtime}:" in runner
            assert f'profiles: ["agent-{runtime}"]' in runner
            assert 'command: ["agent-host"]' in runner
            assert 'test: ["CMD", "diffuse", "agent-host-healthcheck"]' in runner
            assert "DIFFUSE_CONTEXT_SERVICE_URL: http://context-service:8011/agent/v1" in runner
            assert "env_file:" not in runner
            assert "DATABASE_URL" not in runner
        assert "claude_agent_data" not in _service_block(text, "agent-host-codex")
        assert "codex_agent_data" not in _service_block(text, "agent-host-claude")


def test_only_the_credential_free_gateway_shares_the_app_network_with_runners():
    for path in COMPOSE_FILES:
        text = path.read_text()
        app = _service_block(text, "app")
        worker = _service_block(text, "worker")
        gateway = _service_block(text, "context-service")
        assert "agent_mcp_" not in app
        assert "networks: [backend, agent_mcp_claude, agent_mcp_codex]" in gateway
        assert "runner_control_claude" in worker
        assert "runner_control_codex" in worker
        for runtime in ("claude", "codex"):
            assert (
                f"networks: [agent_mcp_{runtime}, agent_egress_{runtime}, "
                f"runner_control_{runtime}]"
            ) in _service_block(text, f"agent-host-{runtime}")


def test_worker_and_app_never_mount_a_runner_credential_volume():
    for path in COMPOSE_FILES:
        text = path.read_text()
        for service in ("worker", "app"):
            block = _service_block(text, service)
            assert "agent_data" not in block
            assert "DIFFUSE_REVIEW_AGENT_HOME" not in block


def test_only_the_worker_retains_dispatch_signing_authority():
    for path in COMPOSE_FILES:
        text = path.read_text()
        assert 'DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY: ""' in _service_block(text, "app")
        assert 'DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY: ""' in _service_block(text, "migrate")
        for runtime in ("claude", "codex"):
            runner = _service_block(text, f"agent-host-{runtime}")
            assert "DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY" not in runner
            assert "DIFFUSE_REVIEW_AGENT_DISPATCH_PUBLIC_KEY" in runner
            assert "DIFFUSE_REVIEW_AGENT_TRANSPORT_SECRET" in runner


def test_runner_images_pin_cli_dependencies_and_the_runtime_keeps_the_home_private():
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()
    assert "AS runner-claude" in dockerfile
    assert "AS runner-codex" in dockerfile
    assert "DISABLE_AUTOUPDATER=1" in dockerfile
    assert 'xyz.intuitum.diffuse.cli.name="claude-code"' in dockerfile
    assert 'xyz.intuitum.diffuse.cli.version="2.1.224"' in dockerfile
    assert 'xyz.intuitum.diffuse.cli.name="codex"' in dockerfile
    assert 'xyz.intuitum.diffuse.cli.version="0.147.0"' in dockerfile
    assert "addgroup --system --gid 10001 diffuse" in dockerfile
    assert "--mode=700 /var/lib/diffuse/agent" in dockerfile
    assert "--mode=700 /var/lib/diffuse/agent/home" in dockerfile
    assert "'10001:10001:700'" in dockerfile
    assert '"@anthropic-ai/claude-code": "2.1.224"' in (
        REPOSITORY_ROOT / "packages" / "host" / "runtimes" / "claude" / "package.json"
    ).read_text()
    assert '"@openai/codex": "0.147.0"' in (
        REPOSITORY_ROOT / "packages" / "host" / "runtimes" / "codex" / "package.json"
    ).read_text()


def test_source_runner_images_are_explicit_prebuilt_overrides_not_implicit_compose_builds():
    """Source Compose defaults locally, while release Compose requires a digest.

    Keeping the source services image-only is intentional: a production-like
    source checkout may point them at a separately built or scanned image, and
    `docker compose up --build` must not silently replace that selection.
    """

    source = (REPOSITORY_ROOT / "compose.yaml").read_text()
    release = (REPOSITORY_ROOT / "deploy" / "compose.yaml").read_text()
    for runtime in ("claude", "codex"):
        variable = f"DIFFUSE_{runtime.upper()}_RUNNER_IMAGE"
        source_runner = _service_block(source, f"agent-host-{runtime}")
        release_runner = _service_block(release, f"agent-host-{runtime}")

        assert f"image: ${{{variable}:-diffuse-runner-{runtime}:local}}" in source_runner
        assert "build:" not in source_runner
        release_image = f"image: ${{{variable}:?Set {variable} to the release digest in .env}}"
        assert release_image in release_runner
        assert "build:" not in release_runner
