import subprocess
from datetime import UTC, datetime

import pytest

from service.repositories import (
    RegisteredRepository,
    repository_clone_url,
    validate_default_branch,
    validate_repository_origin_allowed,
)
from service.repository_indexing import (
    repository_api_base_url,
    repository_index_event,
)
from service.repository_mirror import (
    RepositoryMirror,
    RepositoryMirrorError,
    git_askpass_path,
    max_repository_bytes,
)
from service.scm import validate_repository_name


def _git(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )


def test_clone_url_joins_a_trailing_slash_origin_with_a_nested_name():
    assert (
        repository_clone_url(
            "https://github.example.com/",
            "platform/backend/payments",
        )
        == "https://github.example.com/platform/backend/payments.git"
    )


def test_repository_onboarding_origin_requires_explicit_allowlist(monkeypatch):
    monkeypatch.setenv("GITHUB_WEB_URL", "https://github.com")
    monkeypatch.setenv(
        "GITHUB_ALLOWED_INSTANCES",
        "https://github.enterprise.example",
    )

    assert (
        validate_repository_origin_allowed(
            "github",
            "https://github.enterprise.example/",
        )
        == "https://github.enterprise.example"
    )
    with pytest.raises(ValueError, match="not configured"):
        validate_repository_origin_allowed(
            "github",
            "https://attacker.example",
        )


@pytest.mark.parametrize("provider", ["gitlab", "bitbucket", "GitHub", ""])
def test_repository_onboarding_refuses_any_provider_other_than_github(
    monkeypatch,
    provider,
):
    monkeypatch.setenv("GITHUB_WEB_URL", "https://github.com")

    with pytest.raises(ValueError, match="scm_provider must be github"):
        validate_repository_origin_allowed(provider, "https://github.com")


@pytest.mark.parametrize("branch", ["../main", "feature//unsafe", "main.lock", "a@{b"])
def test_default_branch_rejects_unsafe_ref_names(branch):
    with pytest.raises(ValueError, match="branch"):
        validate_default_branch(branch)


@pytest.mark.parametrize("name", ["../repo", "owner/../repo", "owner", "owner//repo"])
def test_repository_name_rejects_path_traversal_and_incomplete_names(name):
    with pytest.raises(ValueError, match="Repository name"):
        validate_repository_name(name)


def test_mirror_fetches_and_checks_out_exact_commits(tmp_path):
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    mirror_root = tmp_path / "mirrors"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Diffuse Test")
    _git(source, "config", "user.email", "diffuse@example.invalid")
    tracked = source / "value.txt"
    tracked.write_text("first\n")
    _git(source, "add", "value.txt")
    _git(source, "commit", "-m", "first")
    first_commit = _git(source, "rev-parse", "HEAD").stdout.strip()
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        capture_output=True,
        text=True,
        check=True,
    )

    repository = RegisteredRepository(
        id=17,
        scm_provider="github",
        scm_base_url="https://github.com",
        full_name="owner/repo",
        default_branch="main",
        clone_url=str(remote),
        enabled=True,
        mirror_state="unconfigured",
        last_fetched_sha=None,
        last_error_code=None,
    )
    mirror = RepositoryMirror(repository, root=mirror_root)

    assert mirror.resolve_default_commit() == first_commit
    with mirror.checkout(first_commit) as checkout:
        assert (checkout / "value.txt").read_text() == "first\n"
        assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == first_commit

    assert not list(mirror_root.glob(".*-worktree-*"))

    tracked.write_text("second\n")
    _git(source, "add", "value.txt")
    _git(source, "commit", "-m", "second")
    second_commit = _git(source, "rev-parse", "HEAD").stdout.strip()
    _git(source, "push", str(remote), "main")

    assert mirror.resolve_default_commit() == second_commit
    with mirror.checkout(second_commit) as checkout:
        assert (checkout / "value.txt").read_text() == "second\n"


def test_git_environment_exposes_only_the_selected_token(monkeypatch, tmp_path):
    repository = RegisteredRepository(
        id=18,
        scm_provider="github",
        scm_base_url="https://github.com",
        full_name="owner/repo",
        default_branch="main",
        clone_url="https://github.com/owner/repo.git",
        enabled=True,
        mirror_state="unconfigured",
        last_fetched_sha=None,
        last_error_code=None,
    )
    monkeypatch.setenv("GITHUB_TOKEN", "selected-token")
    monkeypatch.setenv("DATABASE_URL", "must-not-reach-git")
    monkeypatch.setenv("OPENAI_KEY", "must-not-reach-git")

    environment = RepositoryMirror(repository, root=tmp_path)._git_environment()

    assert environment["DIFFUSE_GIT_TOKEN"] == "selected-token"
    assert "GITHUB_TOKEN" not in environment
    assert "DATABASE_URL" not in environment
    assert "OPENAI_KEY" not in environment
    assert environment["GIT_CONFIG_KEY_2"] == "core.hooksPath"


def test_packaged_git_askpass_path_must_be_absolute(monkeypatch):
    monkeypatch.setenv("DIFFUSE_GIT_ASKPASS", "relative/askpass.sh")

    with pytest.raises(ValueError, match="must be an absolute path"):
        git_askpass_path()


def test_packaged_git_askpass_path_must_be_executable(monkeypatch, tmp_path):
    helper = tmp_path / "askpass.sh"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o600)
    monkeypatch.setenv("DIFFUSE_GIT_ASKPASS", str(helper))

    with pytest.raises(RepositoryMirrorError, match="missing or not executable"):
        git_askpass_path()


def test_repository_lock_refuses_symbolic_links(tmp_path):
    repository = RegisteredRepository(
        id=19,
        scm_provider="github",
        scm_base_url="https://github.com",
        full_name="owner/repo",
        default_branch="main",
        clone_url="https://github.com/owner/repo.git",
        enabled=True,
        mirror_state="unconfigured",
        last_fetched_sha=None,
        last_error_code=None,
    )
    mirror_root = tmp_path / "mirrors"
    mirror_root.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("unchanged")
    (mirror_root / ".19.lock").symlink_to(unrelated)

    with pytest.raises(RepositoryMirrorError, match="lock safely"):
        RepositoryMirror(repository, root=mirror_root).resolve_default_commit()

    assert unrelated.read_text() == "unchanged"


def test_repository_index_event_uses_enterprise_api_and_exact_commit(monkeypatch):
    repository = RegisteredRepository(
        id=20,
        scm_provider="github",
        scm_base_url="https://github.example.com",
        full_name="owner/repo",
        default_branch="main",
        clone_url="https://github.example.com/owner/repo.git",
        enabled=True,
        mirror_state="ready",
        last_fetched_sha="a" * 40,
        last_error_code=None,
    )
    monkeypatch.setenv("GITHUB_WEB_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")

    event = repository_index_event(
        repository,
        commit_sha="b" * 40,
        requested_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        delivery_id="api-index-test",
    )

    assert repository_api_base_url(repository) == "https://github.example.com/api/v3"
    assert event.before_sha == "a" * 40
    assert event.after_sha == "b" * 40
    assert event.api_base_url == "https://github.example.com/api/v3"


def _bomb_repository(tmp_path, *, payload):
    source = tmp_path / "bomb-source"
    remote = tmp_path / "bomb-remote.git"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Diffuse Test")
    _git(source, "config", "user.email", "diffuse@example.invalid")
    (source / "payload.txt").write_text(payload)
    _git(source, "add", "payload.txt")
    _git(source, "commit", "-m", "payload")
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        capture_output=True,
        text=True,
        check=True,
    )
    return remote, _git(source, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_repository_byte_ceiling_must_be_positive(monkeypatch, value):
    monkeypatch.setenv("DIFFUSE_MAX_REPOSITORY_BYTES", value)

    with pytest.raises(ValueError, match="DIFFUSE_MAX_REPOSITORY_BYTES must be positive"):
        max_repository_bytes()


def test_clone_over_the_byte_ceiling_is_refused_before_it_is_promoted(monkeypatch, tmp_path):
    remote, commit = _bomb_repository(tmp_path, payload="content\n")
    mirror_root = tmp_path / "mirrors"
    repository = RegisteredRepository(
        id=21,
        scm_provider="github",
        scm_base_url="https://github.com",
        full_name="owner/repo",
        default_branch="main",
        clone_url=str(remote),
        enabled=True,
        mirror_state="unconfigured",
        last_fetched_sha=None,
        last_error_code=None,
    )
    monkeypatch.setenv("DIFFUSE_MAX_REPOSITORY_BYTES", "1024")
    mirror = RepositoryMirror(repository, root=mirror_root)

    with (
        pytest.raises(RepositoryMirrorError, match="mirror exceeds the configured size limit"),
        mirror.checkout(commit),
    ):
        pass

    assert not mirror.mirror_path.exists()
    assert not list(mirror_root.glob(".21-clone-*"))


def test_checkout_refuses_a_tree_that_expands_past_the_byte_ceiling(monkeypatch, tmp_path):
    # The payload compresses to a few kilobytes in the pack, so only measuring the
    # expanded tree can catch it.
    remote, commit = _bomb_repository(tmp_path, payload=("x" * 63 + "\n") * 65536)
    mirror_root = tmp_path / "mirrors"
    repository = RegisteredRepository(
        id=22,
        scm_provider="github",
        scm_base_url="https://github.com",
        full_name="owner/repo",
        default_branch="main",
        clone_url=str(remote),
        enabled=True,
        mirror_state="unconfigured",
        last_fetched_sha=None,
        last_error_code=None,
    )
    mirror = RepositoryMirror(repository, root=mirror_root)

    monkeypatch.setenv("DIFFUSE_MAX_REPOSITORY_BYTES", str(64 * 1024 * 1024))
    with mirror.checkout(commit) as checkout:
        assert (checkout / "payload.txt").stat().st_size == 4 * 1024 * 1024

    monkeypatch.setenv("DIFFUSE_MAX_REPOSITORY_BYTES", str(1024 * 1024))
    with (
        pytest.raises(RepositoryMirrorError, match="checkout exceeds the configured size limit"),
        mirror.checkout(commit),
    ):
        pass

    assert not list(mirror_root.glob(".22-worktree-*"))
