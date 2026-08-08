"""Agent operation primitives and the shared CLI-native contract.

`service.agents.contract` is the Gate A runtime/session/capability/result
surface shared by the control plane and the isolated agent-runner. Session
subprocess helpers remain offline-testable; hosted native review calls use
them through the runner, while local CLI-native review remains a later Gate C
step.
"""
