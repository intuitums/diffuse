"""Release automation must publish native, attestable runner manifests."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_runner_manifests_are_assembled_from_native_architecture_builds():
    workflow = WORKFLOW.read_text()
    build, publish = workflow.split("\n  publish:\n", 1)

    assert "- platform: linux/amd64" in build
    assert "- platform: linux/arm64" in build
    assert build.count("platforms: ${{ matrix.platform }}") == 3
    assert "target: runner-claude" in build
    assert "target: runner-codex" in build
    assert "name=${{ env.CLAUDE_RUNNER_IMAGE }},push-by-digest=true" in build
    assert "name=${{ env.CODEX_RUNNER_IMAGE }},push-by-digest=true" in build
    assert "sbom: true" in build
    assert "provenance: mode=max" in build

    # The architecture-neutral publish job may assemble manifests, but must not
    # execute either target through x86 emulation.
    assert "--platform linux/amd64,linux/arm64" not in publish
    assert 'files=("${runtime}-${architecture}-"*)' in publish
    assert 'cosign sign --yes "${image}@${digest}"' in publish
    assert "sh.intuitum.diffuse.runner.claude=" in publish
    assert "sh.intuitum.diffuse.runner.codex=" in publish
