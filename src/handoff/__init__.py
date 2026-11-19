"""Handoff Engine: Git-native task handoff and controlled agent execution.

Layers:
    cli        -> single command entrypoint; writes go through TaskStore
    store      -> Git-backed compare-and-set task and event storage
    worker     -> claim, isolated worktree, agent execution, gates, push and PR
    verifier   -> structured deployment and acceptance checks
    admin      -> rebuildable index and query API
"""

__version__ = "0.1.0"

SCHEMA_VERSION = 1
