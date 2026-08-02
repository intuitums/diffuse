"""The host boundary an agent CLI review runs behind.

None of these spawn a CLI or call a model. They cover the three things that have
to be true before a review may hand an untrusted diff to an agent: the child
environment carries no credential, the sandbox policy carries every key that is
load-bearing, and an install too old to honour that policy is refused rather than
run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from service.cli import agent as agent_cli
from service.cli import review as review_cli
from service.cli.agent import POLICY_COMMAND
from service.review import agent_host
from service.review.agent_host import (
    AGENT_CLIS,
    CLAUDE_CODE,
    CREDENTIAL_ENVIRONMENT,
    AgentHostError,
    agent_config_directory,
    agent_environment,
    agent_home,
    agent_scratch_directory,
    ensure_agent_config_directory,
    parse_version,
    require_supported_platform,
    require_version_floor,
    resolve_cli,
    sandbox_settings,
    sandbox_settings_are_current,
    version_floor_message,
    write_sandbox_settings,
)
from service.review.runtimes import CLAUDE_CODE_RUNTIME, RUNTIME_NAMES


@pytest.fixture
def owned_home(monkeypatch, tmp_path) -> Path:
    """Point the Diffuse-owned agent directory somewhere disposable.

    Every test in this file needs it: the default is `~/.diffuse/agent`, and a
    test suite that writes settings into a developer's real one is a test suite
    that changes how their next review runs.
    """

    home = tmp_path / "agent-home"
    monkeypatch.setenv("DIFFUSE_AGENT_HOME", str(home))
    return home


def _namespace(**values) -> argparse.Namespace:
    return argparse.Namespace(**values)


def _completed(stdout: str = "", *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def installed_cli(monkeypatch, tmp_path):
    """Make `resolve_executable` succeed without an agent CLI on the machine."""

    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("")
    monkeypatch.setattr(agent_host.shutil, "which", lambda name: str(executable))
    return executable


# --- The child environment ------------------------------------------------


def test_credentials_are_absent_from_the_agent_environment(monkeypatch, owned_home):
    """The child environment is built from an allowlist, not filtered by a denylist.

    Named credentials are asserted one by one because they are the ones a reader
    will look for, but the property under test is stronger: nothing outside
    `INHERITED_ENVIRONMENT` survives, so a credential variable nobody thought to
    list is excluded too.
    """

    for name in CREDENTIAL_ENVIRONMENT:
        monkeypatch.setenv(name, "leaked")
    monkeypatch.setenv("A_VARIABLE_NOBODY_LISTED", "leaked")

    with agent_scratch_directory() as scratch:
        environment = agent_environment(CLAUDE_CODE, scratch=scratch)

    for name in CREDENTIAL_ENVIRONMENT:
        assert name not in environment, f"{name} reached the agent process"
    assert "A_VARIABLE_NOBODY_LISTED" not in environment
    assert "leaked" not in environment.values()


def test_agent_environment_inherits_only_the_allowlist(monkeypatch, owned_home):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    with agent_scratch_directory() as scratch:
        environment = agent_environment(CLAUDE_CODE, scratch=scratch)

    assert environment["LANG"] == "en_US.UTF-8"
    # The agent's own process must still reach the model API through a corporate
    # proxy while its Bash subprocesses have no egress at all.
    assert environment["HTTPS_PROXY"] == "http://proxy.internal:3128"


def test_agent_environment_replaces_home_path_and_tmpdir(monkeypatch, owned_home):
    monkeypatch.setenv("HOME", "/Users/developer")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("TMPDIR", "/var/folders/real")

    with agent_scratch_directory() as scratch:
        environment = agent_environment(CLAUDE_CODE, scratch=scratch)
        assert environment["HOME"] == str(scratch)
        assert environment["TMPDIR"] == str(scratch / "tmp")
        # An empty PATH is what stops `gh`, `aws`, and every other
        # credential-bearing tool on the developer's PATH from being resolvable
        # by name from inside the session.
        assert environment["PATH"] == str(scratch / "bin")
        assert not list((scratch / "bin").iterdir())


def test_agent_environment_points_the_cli_at_the_diffuse_directory(owned_home):
    with agent_scratch_directory() as scratch:
        environment = agent_environment(CLAUDE_CODE, scratch=scratch)
    assert environment["CLAUDE_CONFIG_DIR"] == str(owned_home / "claude")


# --- The sandbox policy ---------------------------------------------------


def test_settings_carry_every_hard_gate_key():
    """The four keys whose absence is a silent downgrade rather than an error.

    Each one closes a failure that otherwise looks like a working review: a
    missing sandbox that warns and continues, a model that can ask for the
    sandbox to be switched off, a new domain that prompts (and so hangs) instead
    of being denied, and a default read policy that is the whole machine.
    """

    sandbox = sandbox_settings()["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"]["strictAllowlist"] is True
    assert [entry["path"] for entry in sandbox["credentials"]["files"]]
    assert all(entry["mode"] == "deny" for entry in sandbox["credentials"]["files"])


def test_allowed_domains_is_empty_rather_than_curated():
    """An empty list is worth more than a curated one, not less.

    The sandbox proxy allows by the hostname the client itself claims and does
    not inspect TLS, so every entry on an allowlist is a name to front through.
    """

    assert sandbox_settings()["sandbox"]["network"]["allowedDomains"] == []


def test_deny_read_names_the_real_home_not_a_tilde(monkeypatch):
    """`~` would expand against the child's HOME, which Diffuse has replaced.

    `agent_environment` points the agent's `HOME` at a throwaway scratch
    directory. A policy written as `~/` would therefore deny reads of the scratch
    directory and leave the developer's actual home readable -- the exact
    opposite of what the line is for.
    """

    monkeypatch.setattr(agent_host, "REAL_HOME", Path("/Users/developer"))
    sandbox = sandbox_settings()["sandbox"]

    assert sandbox["filesystem"]["denyRead"] == ["/Users/developer/"]
    denied = {entry["path"] for entry in sandbox["credentials"]["files"]}
    assert denied == {
        "/Users/developer/.ssh",
        "/Users/developer/.aws",
        "/Users/developer/.config/gh",
    }
    assert not any("~" in path for path in denied)


def test_worktree_is_the_only_readable_path_when_supplied(monkeypatch):
    monkeypatch.setattr(agent_host, "REAL_HOME", Path("/Users/developer"))
    filesystem = sandbox_settings("/tmp/review-worktree")["sandbox"]["filesystem"]
    assert filesystem["allowRead"] == ["/tmp/review-worktree"]


def test_persisted_policy_omits_the_worktree(owned_home):
    """The persisted file is the part that does not change per review.

    The worktree differs every run, so the adapter supplies it through
    `--settings` at call time rather than rewriting the user settings file before
    each review.
    """

    written = json.loads(write_sandbox_settings(CLAUDE_CODE).read_text())
    assert "allowRead" not in written["sandbox"]["filesystem"]
    assert written["sandbox"]["filesystem"]["denyRead"]


def test_settings_are_written_only_inside_the_diffuse_directory(owned_home, tmp_path):
    """Diffuse never reads or writes the developer's own CLI configuration."""

    vendor_directory = tmp_path / "vendor-claude"
    vendor_directory.mkdir()

    path = write_sandbox_settings(CLAUDE_CODE)

    assert path == owned_home / "claude" / "settings.json"
    assert path.is_file()
    assert not list(vendor_directory.iterdir())
    assert path.stat().st_mode & 0o777 == 0o600


# --- The configuration directory ------------------------------------------


def test_agent_home_defaults_under_the_diffuse_directory(monkeypatch):
    monkeypatch.delenv("DIFFUSE_AGENT_HOME", raising=False)
    assert agent_home() == Path("~/.diffuse/agent").expanduser()


def test_agent_home_refuses_a_relative_override(monkeypatch):
    monkeypatch.setenv("DIFFUSE_AGENT_HOME", "agent")
    with pytest.raises(AgentHostError, match="absolute path"):
        agent_home()


def test_config_directory_is_created_owner_only(owned_home):
    path = ensure_agent_config_directory(CLAUDE_CODE)
    assert path == agent_config_directory(CLAUDE_CODE)
    assert path.is_dir()
    assert path.stat().st_mode & 0o777 == 0o700


def test_config_directory_is_tightened_when_the_cli_created_it_first(owned_home):
    """The agent CLI makes this directory itself, world-readable, on first run.

    `mkdir(mode=...)` is a no-op for a directory that already exists and is never
    applied to parents, so requesting 0700 at creation is not the same as having
    it. What lands here is a credential.
    """

    directory = agent_config_directory(CLAUDE_CODE)
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    owned_home.chmod(0o755)

    ensure_agent_config_directory(CLAUDE_CODE)

    assert directory.stat().st_mode & 0o777 == 0o700
    assert owned_home.stat().st_mode & 0o777 == 0o700


def test_config_directory_refuses_a_symlink(owned_home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    owned_home.mkdir(parents=True)
    (owned_home / "claude").symlink_to(elsewhere)

    with pytest.raises(AgentHostError, match="must be a real directory"):
        ensure_agent_config_directory(CLAUDE_CODE)


# --- The version floor ----------------------------------------------------


def test_version_floor_is_derived_from_the_capability_table():
    """The floor is the newest requirement, computed rather than remembered.

    Written down as a literal it would be a second copy to keep in step; adding a
    capability with a higher minimum and forgetting to raise a constant is how a
    floor silently stops gating anything.
    """

    assert CLAUDE_CODE.version_floor == max(
        capability.minimum for capability in CLAUDE_CODE.capabilities
    )
    assert CLAUDE_CODE.version_floor_text == "2.1.219"


def test_strict_allowlist_is_what_sets_the_floor():
    """Names the specific reason 2.1.219, not 2.1.216, is the number."""

    newest = max(CLAUDE_CODE.capabilities, key=lambda item: item.minimum)
    assert newest.setting == "network.strictAllowlist"


def test_parse_version_reads_the_vendor_line():
    assert parse_version("2.1.220 (Claude Code)") == (2, 1, 220)
    assert parse_version("codex-cli 0.146.0") == (0, 146, 0)


def test_parse_version_refuses_output_with_no_version():
    with pytest.raises(AgentHostError, match="could not read a version number"):
        parse_version("command not found")


def test_version_floor_refuses_an_older_build(monkeypatch, owned_home, installed_cli):
    monkeypatch.setattr(
        agent_host.subprocess, "run", lambda *a, **k: _completed("2.1.205 (Claude Code)")
    )
    with pytest.raises(AgentHostError) as error:
        require_version_floor(CLAUDE_CODE)

    message = str(error.value)
    # A version number alone is an instruction; the consequence is the reason.
    assert "network.strictAllowlist" in message
    assert "filesystem.disabled" in message
    assert "hang" in message
    # Capabilities the installed build already has must not be listed as missing.
    assert "--json-schema" not in message
    assert "sandbox.credentials" not in message


def test_version_floor_accepts_the_measured_build(monkeypatch, owned_home, installed_cli):
    monkeypatch.setattr(
        agent_host.subprocess, "run", lambda *a, **k: _completed("2.1.220 (Claude Code)")
    )
    assert require_version_floor(CLAUDE_CODE) == (2, 1, 220)


def test_version_floor_message_lists_every_missing_capability():
    message = version_floor_message(CLAUDE_CODE, (2, 1, 100))
    for capability in CLAUDE_CODE.capabilities:
        assert capability.setting in message


def test_missing_cli_names_the_executable_and_the_way_out(monkeypatch, owned_home):
    monkeypatch.setattr(agent_host.shutil, "which", lambda name: None)
    with pytest.raises(AgentHostError) as error:
        require_version_floor(CLAUDE_CODE)
    assert "claude" in str(error.value)
    assert "REVIEW_RUNTIME=litellm" in str(error.value)


def test_version_probe_reports_a_hang_rather_than_waiting(monkeypatch, owned_home, installed_cli):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=30)

    monkeypatch.setattr(agent_host.subprocess, "run", hang)
    with pytest.raises(AgentHostError, match="did not respond"):
        require_version_floor(CLAUDE_CODE)


# --- Platform support -----------------------------------------------------


def test_native_windows_is_refused(monkeypatch):
    """Refusing beats running with no OS boundary at all."""

    monkeypatch.setattr(agent_host.sys, "platform", "win32")
    with pytest.raises(AgentHostError, match="does not sandbox on native Windows"):
        require_supported_platform()


def test_supported_platform_passes(monkeypatch):
    monkeypatch.setattr(agent_host.sys, "platform", "darwin")
    require_supported_platform()


# --- Status and selection -------------------------------------------------


def test_every_hosted_agent_runtime_has_a_cli():
    """An agent runtime `REVIEW_RUNTIME` accepts must have host plumbing behind it.

    The complement of `test_every_advertised_runtime_resolves`: that one proves a
    named runtime has an adapter, this one proves it has a configuration
    directory, a version floor, and a way to sign in.
    """

    hosted = {cli.runtime for cli in AGENT_CLIS}
    for name in RUNTIME_NAMES:
        if name == "litellm":
            continue
        assert name in hosted, f"{name} is selectable but has no agent host entry"


def test_claude_code_entry_uses_the_runtime_name():
    assert CLAUDE_CODE.runtime == CLAUDE_CODE_RUNTIME
    assert resolve_cli(CLAUDE_CODE_RUNTIME) is CLAUDE_CODE


def test_resolve_cli_refuses_an_unhosted_name():
    with pytest.raises(AgentHostError, match="is not an agent CLI Diffuse hosts"):
        resolve_cli("codex")


def test_status_reports_a_missing_cli_without_failing(monkeypatch, owned_home):
    monkeypatch.setattr(agent_host.shutil, "which", lambda name: None)
    status = agent_host.agent_status()

    assert status["schema_version"] == "diffuse-agent-status-v1"
    assert status["agent_home"] == str(owned_home)
    entry = status["runtimes"][0]
    assert entry["installed"] is False
    assert entry["ready"] is False
    assert "not installed" in entry["problem"]


def test_status_reports_a_ready_runtime(monkeypatch, owned_home, installed_cli):
    write_sandbox_settings(CLAUDE_CODE)

    def fake_run(command, **kwargs):
        if command[1:] == ["--version"]:
            return _completed("2.1.220 (Claude Code)")
        return _completed(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))

    monkeypatch.setattr(agent_host.subprocess, "run", fake_run)
    entry = agent_host.agent_status()["runtimes"][0]

    assert entry["version"] == "2.1.220"
    assert entry["meets_version_floor"] is True
    assert entry["authenticated"] is True
    assert entry["sandbox_settings_written"] is True
    assert entry["sandbox_settings_current"] is True
    assert entry["ready"] is True
    assert "problem" not in entry


def test_a_policy_that_no_longer_matches_is_not_ready(monkeypatch, owned_home, installed_cli):
    """A signed-in developer with a stale policy file is not ready.

    `write_sandbox_settings` runs at login, once, and the file is then read on
    every review for as long as that login lasts. A capability added to the
    table, a tightened credential deny, or a different REAL_HOME all leave a
    weaker boundary on disk than Diffuse intends -- and existence alone cannot
    tell the difference.
    """

    path = write_sandbox_settings(CLAUDE_CODE)
    weakened = json.loads(path.read_text())
    weakened["sandbox"]["network"]["allowedDomains"] = ["*"]
    path.write_text(json.dumps(weakened, indent=2, sort_keys=True) + "\n")

    def fake_run(command, **kwargs):
        if command[1:] == ["--version"]:
            return _completed("2.1.220 (Claude Code)")
        return _completed(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))

    monkeypatch.setattr(agent_host.subprocess, "run", fake_run)
    entry = agent_host.agent_status()["runtimes"][0]

    assert entry["authenticated"] is True
    assert entry["sandbox_settings_written"] is True
    assert entry["sandbox_settings_current"] is False
    assert entry["ready"] is False
    # Pinned to the real subcommand, so renaming it in `service/cli/agent.py`
    # without updating the remedy fails here rather than sending an operator to
    # a command that does not exist.
    assert POLICY_COMMAND in entry["problem"]
    # The remedy must not be "sign in again": they are signed in, and signing in
    # again would not rewrite a file whose contents drifted.
    assert "login" not in entry["problem"]


def test_a_rewritten_policy_becomes_current_again(owned_home, installed_cli):
    path = write_sandbox_settings(CLAUDE_CODE)
    path.write_text("{}\n")
    assert sandbox_settings_are_current(CLAUDE_CODE) is False

    write_sandbox_settings(CLAUDE_CODE)
    assert sandbox_settings_are_current(CLAUDE_CODE) is True


def test_a_missing_policy_is_not_current(owned_home, installed_cli):
    assert sandbox_settings_are_current(CLAUDE_CODE) is False


def test_status_reports_an_unauthenticated_runtime(monkeypatch, owned_home, installed_cli):
    write_sandbox_settings(CLAUDE_CODE)

    def fake_run(command, **kwargs):
        if command[1:] == ["--version"]:
            return _completed("2.1.220 (Claude Code)")
        return _completed(json.dumps({"loggedIn": False, "authMethod": "none"}))

    monkeypatch.setattr(agent_host.subprocess, "run", fake_run)
    entry = agent_host.agent_status()["runtimes"][0]

    assert entry["authenticated"] is False
    assert entry["ready"] is False
    assert "diffuse agent login claude-code" in entry["problem"]


def test_auth_status_reads_the_diffuse_directory(monkeypatch, owned_home, installed_cli):
    """The probe runs against Diffuse's directory, never the developer's.

    A signed-in terminal CLI and a signed-out Diffuse directory are the normal
    state right up until the developer runs `diffuse agent login`, so a probe
    that read the wrong directory would report ready when it is not.
    """

    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["env"] = kwargs["env"]
        return _completed(json.dumps({"loggedIn": True}))

    monkeypatch.setattr(agent_host.subprocess, "run", fake_run)
    agent_host.authentication_status(CLAUDE_CODE)

    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(owned_home / "claude")


def test_unreadable_auth_output_is_not_read_as_signed_in(monkeypatch, owned_home, installed_cli):
    monkeypatch.setattr(agent_host.subprocess, "run", lambda *a, **k: _completed("not json"))
    assert agent_host.authentication_status(CLAUDE_CODE)["logged_in"] is False


# --- Login ----------------------------------------------------------------


def test_login_keeps_the_developers_environment_but_moves_the_config_dir(
    monkeypatch, owned_home, installed_cli
):
    """Sign-in is an interactive browser flow, so it is deliberately not sandboxed.

    Only the configuration directory is overridden. Stripping `HOME`, `PATH`, and
    the terminal the way a review does would break the browser handoff, and there
    is no untrusted diff in the room: the developer typed the command.
    """

    monkeypatch.setenv("HOME", "/Users/developer")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return _completed()

    monkeypatch.setattr(agent_host.subprocess, "run", fake_run)
    assert agent_host.login(CLAUDE_CODE) == 0

    assert seen["command"][1:] == ["auth", "login"]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(owned_home / "claude")
    assert seen["env"]["HOME"] == "/Users/developer"
    assert seen["env"]["PATH"] == "/usr/local/bin:/usr/bin"


def test_login_writes_the_sandbox_policy(monkeypatch, owned_home, installed_cli):
    """A signed-in directory with no policy in it is a review with no boundary."""

    monkeypatch.setattr(agent_host.subprocess, "run", lambda *a, **k: _completed())
    agent_host.login(CLAUDE_CODE)
    assert (owned_home / "claude" / "settings.json").is_file()


def test_failed_login_is_reported(monkeypatch, owned_home, installed_cli):
    monkeypatch.setattr(agent_host.subprocess, "run", lambda *a, **k: _completed(returncode=1))
    with pytest.raises(RuntimeError, match="exited 1"):
        agent_cli._login(_namespace(runtime=CLAUDE_CODE_RUNTIME))


# --- The command line -----------------------------------------------------


def test_cli_routes_agent_commands():
    parser = review_cli._parser()

    status = parser.parse_args(["agent", "status"])
    assert status.command == "agent"
    assert status.agent_command == "status"
    assert status.handler is agent_cli._status

    login = parser.parse_args(["agent", "login", "claude-code"])
    assert login.runtime == "claude-code"
    assert login.handler is agent_cli._login


def test_cli_refuses_an_unhosted_runtime_at_parse_time(capsys):
    """`codex` is a name the plan promises and the host does not yet have.

    Rejecting it in argparse means the developer sees the choices, rather than
    signing in successfully to a runtime `REVIEW_RUNTIME` will not accept.
    """

    parser = review_cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["agent", "login", "codex"])
    assert "claude-code" in capsys.readouterr().err
