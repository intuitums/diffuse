"""Agent operation primitives and the shared CLI-native contract.

`service.agents.contract` is the Gate A runtime/session/capability/result
surface shared by the control plane and the isolated agent-runner. Session
subprocess helpers in this package remain offline-testable and have no
production review callers yet; Gate B/C wire the runner onto the contract.
"""
