"""Per-channel workspace policy.

One behavioural profile today — **prod**: the ``brain_sdk`` SRE brain with the
full tool palette and the global workspace/worktree roots. ``tool_scope=None``
means "no extra gate"; the brain keeps every tool it has.

``WorkspacePolicy`` stays a generic, per-channel extension point: the scope-gate
machinery in ``permission.py`` and the ``repo_roots``/``subagents`` fields let a
future channel be given a different prompt, a clamped tool set, extra readable
roots, or a subset of sub-agents — set a non-default policy and route it in
``resolve_policy``. Nothing routes off prod right now.

The policy is resolved from the channel **ID** (the value persisted on the
thread row), not the name, because the brain options factory only has the ID at
session-open time and resolving the name there would cost a Slack API round-trip
on a hot path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspacePolicy:
    name: str
    # Prompt file (without .md) loaded as the brain system prompt for this tier.
    system_prompt: str
    # Allow-set of bare tool names; None = no gate (allow everything, prod default).
    tool_scope: frozenset[str] | None
    # Extra readable roots beyond the global workspace/worktree dirs.
    repo_roots: tuple[str, ...]
    # Sub-agent names exposed to this tier; None = all registered sub-agents.
    subagents: tuple[str, ...] | None


PROD_POLICY = WorkspacePolicy(
    name="prod",
    system_prompt="brain_sdk",
    tool_scope=None,
    repo_roots=(),
    subagents=None,
)


def resolve_policy(channel: str | None) -> WorkspacePolicy:  # noqa: ARG001
    """Return the policy for a Slack channel ID. Every channel is prod today;
    the ``channel`` arg is kept so a future tier can route off it here."""
    return PROD_POLICY
