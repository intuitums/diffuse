"""Investigation primitives shared by Diffuse and isolated Agent Hosts.

`diffuse_protocol` defines Review Agents, access grants, and structured
candidate/verifier results. Subprocess helpers remain offline-testable; hosted
reviews use them only through an Agent Host. Local-branch review stays deferred
until it can receive the same access grant as a pull-request investigation.
"""
