"""Agent integration: detection/state machine, read-only directory scanner, and one-shot
bounded analysis calls through the codex / claude CLIs. No resident proxies."""

from .base import KINDS, AgentConnection, load_connections, save_connections

__all__ = ["KINDS", "AgentConnection", "load_connections", "save_connections"]
