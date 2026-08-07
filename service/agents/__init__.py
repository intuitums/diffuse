"""CLI-native agent session contracts and offline-testable primitives.

Target architecture: the control plane mints a short-lived capability; an
isolated agent-runner executes Claude Code or Codex; Diffuse validates
`AgentSessionResult` and alone publishes.

Landed here today:

* `capability` / `result` — Gate A contracts shared by worker and runner
* `session` / `claude_session` / `profiles` / `replay` — subprocess primitive
  with record/replay (DEV-329); still has no production callers

Do not add LiteLLM call sites in this package. Do not spawn a CLI from the
worker — production execution belongs to the dedicated runner (Gate B/C).
"""
