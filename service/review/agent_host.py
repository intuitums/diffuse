"""Host plumbing for the agent-CLI review runtimes.

Everything that must be in place before a review may spawn an agent CLI, and
nothing that spawns one: the configuration directory Diffuse owns, the child
environment, the sandbox policy written into that directory, and the version
floor that decides whether the policy is honoured at all. Building argv and
driving a session belongs to the adapter; nothing here calls a model.

Three boundaries stack here and none of them subsumes another:

1. `agent_environment` builds the child environment from an allowlist, so a
   credential Diffuse never names cannot reach the CLI. This is
   `RepositoryMirror._git_environment` (`service/hosted/repository_mirror.py`)
   applied to a second untrusted subprocess.
2. `sandbox_settings` is the CLI's own OS sandbox: no Bash egress, no reads
   outside the worktree, no credential files.
3. `require_version_floor` exists because (2) is version-gated. An older build
   parses a settings key it does not know and drops it *silently*, so the
   review runs against a weaker boundary than the policy on disk describes and
   nothing says so. The floor is the only way to know the policy was enforced.

The sandbox is a real OS boundary -- Seatbelt on macOS, bubblewrap on Linux --
but not a complete one. Its documented scope is Bash subprocesses, and MCP
servers run outside it entirely with full host privileges, which is why the
adapter must always pass `--strict-mcp-config` and why Diffuse's own MCP server
must never execute repository-supplied content.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from service.review.runtimes import CLAUDE_CODE_RUNTIME

#: Where Diffuse keeps the agent configuration it owns. Deliberately not
#: `~/.claude` or `~/.codex`: Diffuse never reads or writes the developer's own
#: CLI configuration, which makes "your terminal CLI is untouched" a guarantee
#: rather than a flag dance. It also means Diffuse's process creates its own
#: credential in its own directory and owns the macOS Keychain ACL for it.
AGENT_HOME_VARIABLE = "DIFFUSE_AGENT_HOME"
DEFAULT_AGENT_HOME = "~/.diffuse/agent"

#: Read once, at the top of the process, before anything rewrites `HOME` for a
#: child. The sandbox policy has to name the *developer's* home directory, and
#: `agent_environment` points the child's `HOME` at a scratch directory -- so a
#: literal `~/` inside the policy would expand to the scratch directory and deny
#: nothing that matters.
REAL_HOME = Path.home()

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")

#: Long enough for a cold native binary on a loaded machine, short enough that a
#: hung probe is not mistaken for a slow review.
VERSION_PROBE_TIMEOUT_SECONDS = 30


class AgentHostError(RuntimeError):
    """The host cannot run an agent CLI review, and says which requirement failed."""


@dataclass(frozen=True)
class AgentCapability:
    """A setting that is version-gated and fails silently below its minimum."""

    setting: str
    minimum: tuple[int, int, int]
    #: What actually happens on an older build. This is the half worth printing:
    #: "upgrade to 2.1.219" is an instruction, "otherwise a new domain prompts
    #: and a prompt in headless mode is a hang" is a reason.
    consequence: str

    @property
    def minimum_text(self) -> str:
        return ".".join(str(part) for part in self.minimum)


#: Measured against Claude Code 2.1.220. Every one of these is honoured on a new
#: enough build and ignored without comment on an older one, which is why the
#: floor below is derived from this table instead of being written down as a
#: number somebody has to remember to raise.
CLAUDE_CODE_CAPABILITIES: tuple[AgentCapability, ...] = (
    AgentCapability(
        "sandbox.credentials",
        (2, 1, 187),
        "the credentials block is inert and the sandbox reads ~/.ssh by default",
    ),
    AgentCapability(
        "credentials.envVars",
        (2, 1, 199),
        "GH_TOKEN and GITHUB_TOKEN are not masked from sandboxed commands",
    ),
    AgentCapability(
        "--json-schema",
        (2, 1, 205),
        "an invalid schema is ignored and the review returns unstructured text",
    ),
    AgentCapability(
        "filesystem.disabled",
        (2, 1, 216),
        "the filesystem policy cannot be closed and defaults to the whole machine",
    ),
    AgentCapability(
        "network.strictAllowlist",
        (2, 1, 219),
        "the sandbox prompts on a new domain instead of denying it, and a prompt "
        "in a headless review is a hang",
    ),
)


@dataclass(frozen=True)
class AgentCli:
    """One agent CLI Diffuse knows how to host."""

    runtime: str
    executable: str
    display_name: str
    #: The directory the vendor's own CLI reads its configuration and credentials
    #: from, which is what lets Diffuse own a second, separate login.
    config_directory_variable: str
    directory_name: str
    capabilities: tuple[AgentCapability, ...]
    #: Argument vectors for the vendor's own auth commands. Diffuse drives these
    #: rather than reimplementing a login: the credential stays the vendor's.
    login_arguments: tuple[str, ...]
    logout_arguments: tuple[str, ...]
    auth_status_arguments: tuple[str, ...]
    upgrade_hint: str

    @property
    def version_floor(self) -> tuple[int, int, int]:
        return max(capability.minimum for capability in self.capabilities)

    @property
    def version_floor_text(self) -> str:
        return ".".join(str(part) for part in self.version_floor)

    def missing_capabilities(self, version: tuple[int, int, int]) -> tuple[AgentCapability, ...]:
        return tuple(item for item in self.capabilities if version < item.minimum)


CLAUDE_CODE = AgentCli(
    runtime=CLAUDE_CODE_RUNTIME,
    executable="claude",
    display_name="Claude Code",
    config_directory_variable="CLAUDE_CONFIG_DIR",
    directory_name="claude",
    capabilities=CLAUDE_CODE_CAPABILITIES,
    login_arguments=("auth", "login"),
    logout_arguments=("auth", "logout"),
    auth_status_arguments=("auth", "status", "--json"),
    upgrade_hint="claude update",
)

#: Every CLI `diffuse agent` will act on. Codex is absent deliberately, on the
#: same rule `RUNTIME_NAMES` follows: a name is listed once it works, not once it
#: is planned. Its version floor has not been established -- nothing has been run
#: against it -- and its sandbox configuration is TOML with a different shape
#: than the JSON below, so signing a developer in would authenticate them for a
#: runtime `REVIEW_RUNTIME` does not accept.
AGENT_CLIS: tuple[AgentCli, ...] = (CLAUDE_CODE,)

#: Inherited verbatim by the agent process. Everything absent from this set is
#: absent from the child by construction, which is the point: an allowlist keeps
#: a credential Diffuse has never heard of out of the subprocess, where a deny
#: list would only keep out the ones somebody remembered. `HOME`, `PATH`, and
#: `TMPDIR` are deliberately not here -- `agent_environment` replaces all three.
INHERITED_ENVIRONMENT = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        # The agent's own process must reach the model API even while its Bash
        # subprocesses have no egress at all; the two layers are independent. On
        # a network that only exits through a proxy, dropping these would fail
        # the review itself rather than harden it.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)

#: Named here only so a test can assert their absence by name. The allowlist
#: above already excludes them, and it excludes the ones nobody listed too.
CREDENTIAL_ENVIRONMENT = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "SSH_AUTH_SOCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DATABASE_URL",
    "DIFFUSE_GIT_TOKEN",
    # The control plane's own secrets. `diffuse review` runs on a developer's
    # machine and normally holds none of these, but the same host plumbing is
    # what a server-side runtime would reuse, and that process holds all three.
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "DIFFUSE_API_TOKEN",
    "POSTGRES_PASSWORD",
)


def require_supported_platform() -> None:
    """Refuse where the CLI sandbox does not exist, rather than run without one.

    Claude Code's sandbox is unsupported on native Windows. Running there would
    mean handing an untrusted diff to an agent with no OS boundary at all, which
    is worse than not offering the runtime.
    """

    if sys.platform.startswith("win"):
        raise AgentHostError(
            "The agent CLI review runtimes need an OS sandbox, and Claude Code "
            "does not sandbox on native Windows. Use WSL2, or set "
            "REVIEW_RUNTIME=litellm."
        )


def agent_home() -> Path:
    """The directory tree Diffuse owns for agent configuration."""

    configured = os.environ.get(AGENT_HOME_VARIABLE, "").strip()
    path = Path(configured).expanduser() if configured else Path(DEFAULT_AGENT_HOME).expanduser()
    if not path.is_absolute():
        raise AgentHostError(f"{AGENT_HOME_VARIABLE} must be an absolute path")
    return path


def agent_config_directory(cli: AgentCli) -> Path:
    return agent_home() / cli.directory_name


def ensure_agent_config_directory(cli: AgentCli) -> Path:
    """Create the CLI's configuration directory, owner-only and not a symlink.

    The permissions are applied rather than only requested. `mkdir(mode=...)` is
    ignored for a directory that already exists and is not applied to parents at
    all, and the agent CLI creates this directory itself the first time it runs
    -- with a world-readable mode. Since what lands here is a credential, the
    directory Diffuse hands it has to be 0700 whoever created it.
    """

    home = agent_home()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    path = agent_config_directory(cli)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise AgentHostError(f"{path} must be a real directory")
    path.mkdir(exist_ok=True)
    path.chmod(0o700)
    return path


def sandbox_settings(worktree: Path | str | None = None) -> dict[str, object]:
    """The sandbox policy Diffuse runs an agent CLI under.

    Four keys are load-bearing, and each closes a failure that is silent rather
    than loud:

    - `failIfUnavailable` -- without it a missing bubblewrap makes the CLI warn
      and then run *unsandboxed*. A review tool must not downgrade itself to no
      boundary and carry on.
    - `allowUnsandboxedCommands: false` -- closes the retry that would otherwise
      let the model ask for its own sandbox to be turned off.
    - `network.strictAllowlist` -- without it a new domain *prompts*, and a
      prompt in a headless review is a hang rather than a denial.
    - `credentials.files` -- the default read policy is the whole machine and
      there is no built-in credential deny list. Omit this and the sandbox reads
      the developer's SSH keys.

    `allowedDomains` is empty rather than curated on purpose. The proxy allows by
    the client's own claimed hostname and does not inspect TLS, so any allowed
    domain is a name to front through; an empty list has nothing to front.

    A repository's own `.claude/settings.json` cannot override `strictAllowlist`
    or the filesystem policy -- those are honoured only from user, managed, and
    `--settings` sources. Since the repository under review is the untrusted
    input, that is what makes writing this to a Diffuse-owned user directory
    safe.
    """

    filesystem: dict[str, object] = {"denyRead": [f"{REAL_HOME}/"]}
    if worktree is not None:
        filesystem["allowRead"] = [str(Path(worktree))]
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "filesystem": filesystem,
            "credentials": {
                "files": [
                    {"path": str(REAL_HOME / ".ssh"), "mode": "deny"},
                    {"path": str(REAL_HOME / ".aws"), "mode": "deny"},
                    {"path": str(REAL_HOME / ".config" / "gh"), "mode": "deny"},
                ],
                "envVars": [
                    {"name": "GH_TOKEN", "mode": "deny"},
                    {"name": "GITHUB_TOKEN", "mode": "deny"},
                ],
            },
        }
    }


def rendered_sandbox_settings() -> str:
    """The exact bytes `write_sandbox_settings` persists.

    One renderer for both writing and checking, so "is the file current" cannot
    answer differently from "what would we write".
    """

    return json.dumps(sandbox_settings(), indent=2, sort_keys=True) + "\n"


def write_sandbox_settings(cli: AgentCli) -> Path:
    """Persist the worktree-independent policy as the CLI's user settings.

    Persisted rather than passed inline on every call so that an operator can
    read what an agent review actually runs under. The worktree is the one part
    that cannot live here -- it differs per review -- so the adapter adds
    `allowRead` through `--settings` at call time.
    """

    directory = ensure_agent_config_directory(cli)
    path = directory / "settings.json"
    path.write_text(rendered_sandbox_settings())
    path.chmod(0o600)
    return path


def sandbox_settings_are_current(cli: AgentCli) -> bool:
    """Whether the persisted policy is the one Diffuse would write today.

    Existence is not the question. This file is written by `diffuse agent
    login`, which a developer runs once, and it is then read on every review
    for as long as the login lasts. Anything that changes the rendered policy
    afterwards -- a capability added to the table, a tightened credential deny,
    a different `REAL_HOME` because the tool moved machines -- leaves a file on
    disk that is weaker than what Diffuse intends, with nothing saying so.

    That is the same silent-weakening this module's version floor exists to
    prevent, one layer up: the floor proves the CLI *can* honour the policy,
    and this proves the policy it will read is the current one.
    """

    path = agent_config_directory(cli) / "settings.json"
    if not path.is_file():
        return False
    try:
        return path.read_text() == rendered_sandbox_settings()
    except OSError:
        return False


@contextmanager
def agent_scratch_directory() -> Iterator[Path]:
    """A throwaway `HOME` and `PATH` for one agent process."""

    with tempfile.TemporaryDirectory(prefix="diffuse-agent-") as raw:
        scratch = Path(raw)
        (scratch / "bin").mkdir(mode=0o700)
        (scratch / "tmp").mkdir(mode=0o700)
        yield scratch


def agent_environment(cli: AgentCli, *, scratch: Path) -> dict[str, str]:
    """Build the agent process's environment from nothing but the allowlist.

    `PATH` points at an empty scratch directory so that `gh`, `aws`, and every
    other credential-bearing tool on the developer's `PATH` cannot be resolved by
    name from inside the session. Diffuse resolves the agent CLI to an absolute
    path itself before spawning it, so the empty `PATH` costs nothing; the CLIs
    Diffuse hosts are native binaries and need no interpreter looked up.

    `HOME` points there too, so anything the CLI reads out of a home directory
    Diffuse did not anticipate finds an empty one. The configuration directory is
    named explicitly and so is unaffected.
    """

    environment = {key: value for key, value in os.environ.items() if key in INHERITED_ENVIRONMENT}
    environment.update(
        {
            "HOME": str(scratch),
            "PATH": str(scratch / "bin"),
            "TMPDIR": str(scratch / "tmp"),
            cli.config_directory_variable: str(agent_config_directory(cli)),
        }
    )
    return environment


def resolve_executable(cli: AgentCli) -> Path:
    """Find the CLI on the *parent's* `PATH`, which the child will not have."""

    found = shutil.which(cli.executable)
    if not found:
        raise AgentHostError(
            f"{cli.display_name} is not installed: `{cli.executable}` is not on PATH. "
            f"Install it, or set REVIEW_RUNTIME=litellm."
        )
    return Path(found)


def _run_cli(
    cli: AgentCli,
    arguments: tuple[str, ...],
    *,
    timeout: int = VERSION_PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a read-only vendor subcommand against the Diffuse-owned config dir."""

    executable = resolve_executable(cli)
    with agent_scratch_directory() as scratch:
        try:
            return subprocess.run(
                [str(executable), *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=agent_environment(cli, scratch=scratch),
            )
        except subprocess.TimeoutExpired as error:
            raise AgentHostError(
                f"{cli.display_name} did not respond to "
                f"`{cli.executable} {' '.join(arguments)}` within {timeout}s"
            ) from error


def parse_version(text: str) -> tuple[int, int, int]:
    """Read the first dotted triple out of a `--version` line."""

    match = _VERSION.search(text)
    if not match:
        raise AgentHostError(f"could not read a version number from {text.strip()!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def probe_version(cli: AgentCli) -> tuple[int, int, int]:
    """Ask the installed CLI what version it is."""

    completed = _run_cli(cli, ("--version",))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise AgentHostError(
            f"`{cli.executable} --version` exited {completed.returncode}: {detail}"
        )
    return parse_version(completed.stdout)


def version_floor_message(cli: AgentCli, version: tuple[int, int, int]) -> str:
    """Name every capability the installed build silently lacks, and its cost."""

    installed = ".".join(str(part) for part in version)
    missing = cli.missing_capabilities(version)
    lines = [
        f"{cli.display_name} {installed} is installed, but Diffuse requires "
        f"{cli.version_floor_text} or newer. These settings are accepted and then "
        f"ignored on {installed}, so a review would run under a weaker boundary "
        f"than the policy on disk describes:",
    ]
    lines.extend(
        f"  {item.setting} (needs {item.minimum_text}) -- {item.consequence}" for item in missing
    )
    lines.append(f"Upgrade with `{cli.upgrade_hint}`, or set REVIEW_RUNTIME=litellm.")
    return "\n".join(lines)


def require_version_floor(cli: AgentCli) -> tuple[int, int, int]:
    """Refuse an install too old to honour the policy, naming what it would drop."""

    version = probe_version(cli)
    if version < cli.version_floor:
        raise AgentHostError(version_floor_message(cli, version))
    return version


def authentication_status(cli: AgentCli) -> dict[str, object]:
    """Ask the vendor CLI whether the Diffuse-owned directory is signed in.

    Reads the vendor's own answer rather than looking for a credential file:
    Claude Code stores its credential in the macOS Keychain on a Mac and on disk
    elsewhere, so a file probe would report a signed-in developer as signed out
    on the one platform this runtime is most used from.
    """

    def unauthenticated(detail: str | None) -> dict[str, object]:
        return {"logged_in": False, "auth_method": None, "account": None, "detail": detail}

    completed = _run_cli(cli, cli.auth_status_arguments)
    if completed.returncode != 0:
        return unauthenticated((completed.stderr or "").strip() or None)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return unauthenticated("unreadable auth status output")
    # One shape on every path. The keys are read by `cli_status` and printed by
    # `diffuse agent status`, and a key that appears only on the failure branch
    # is a KeyError waiting for the first caller who does not use `.get`.
    return {
        "logged_in": bool(payload.get("loggedIn")),
        "auth_method": payload.get("authMethod"),
        "account": payload.get("email"),
        "detail": None,
    }


def resolve_cli(name: str) -> AgentCli:
    known = {item.runtime: item for item in AGENT_CLIS}
    if name not in known:
        raise AgentHostError(
            f"{name!r} is not an agent CLI Diffuse hosts; use one of: {', '.join(sorted(known))}"
        )
    return known[name]


def login(cli: AgentCli) -> int:
    """Drive the vendor's own sign-in into the Diffuse-owned directory.

    Deliberately *not* run under `agent_environment`: this is an interactive
    browser flow the developer is watching, so it needs their real `HOME`,
    `PATH`, and terminal. Only the configuration directory is overridden, which
    is the whole point -- the credential lands in Diffuse's directory and the
    developer's own `~/.claude` is never read or written.
    """

    require_supported_platform()
    executable = resolve_executable(cli)
    directory = ensure_agent_config_directory(cli)
    write_sandbox_settings(cli)
    environment = dict(os.environ)
    environment[cli.config_directory_variable] = str(directory)
    completed = subprocess.run(
        [str(executable), *cli.login_arguments],
        env=environment,
        check=False,
    )
    return completed.returncode


def cli_status(cli: AgentCli) -> dict[str, object]:
    """Everything checkable about one CLI without making a model call."""

    status: dict[str, object] = {
        "runtime": cli.runtime,
        "executable": cli.executable,
        "config_directory": str(agent_config_directory(cli)),
        "version_floor": cli.version_floor_text,
        "required_settings": [item.setting for item in cli.capabilities],
    }
    try:
        executable = resolve_executable(cli)
    except AgentHostError as error:
        return {**status, "installed": False, "ready": False, "problem": str(error)}
    status["installed"] = True
    status["path"] = str(executable)

    try:
        version = probe_version(cli)
    except AgentHostError as error:
        return {**status, "ready": False, "problem": str(error)}
    status["version"] = ".".join(str(part) for part in version)
    status["meets_version_floor"] = version >= cli.version_floor
    if version < cli.version_floor:
        return {**status, "ready": False, "problem": version_floor_message(cli, version)}

    settings_path = agent_config_directory(cli) / "settings.json"
    settings_written = settings_path.is_file()
    settings_current = sandbox_settings_are_current(cli)
    status["sandbox_settings_written"] = settings_written
    status["sandbox_settings_current"] = settings_current

    authentication = authentication_status(cli)
    status["authenticated"] = authentication["logged_in"]
    status["auth_method"] = authentication.get("auth_method")
    status["account"] = authentication.get("account")

    ready = bool(authentication["logged_in"]) and settings_current
    status["ready"] = ready
    if not ready:
        # A stale policy is its own problem with its own remedy. Telling a
        # signed-in developer to sign in again would be wrong and would not fix
        # it: `diffuse agent write-policy` is what rewrites the file.
        if settings_written and not settings_current:
            status["problem"] = (
                "the sandbox policy on disk is not the one Diffuse writes today, so a "
                "review would run under a boundary that does not match this version; "
                f"run `diffuse agent write-policy {cli.runtime}`"
            )
        else:
            status["problem"] = (
                f"run `diffuse agent login {cli.runtime}` to sign in and write the "
                "sandbox policy"
            )
    return status


def agent_status() -> dict[str, object]:
    """The `diffuse agent status` document."""

    platform_problem: str | None = None
    try:
        require_supported_platform()
    except AgentHostError as error:
        platform_problem = str(error)
    return {
        "schema_version": "diffuse-agent-status-v1",
        "agent_home": str(agent_home()),
        "platform_supported": platform_problem is None,
        "platform_problem": platform_problem,
        "runtimes": [cli_status(cli) for cli in AGENT_CLIS],
    }
