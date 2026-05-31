"""Per-channel workspace policy — the tier split.

One bot process, two behavioural profiles selected by Slack channel:

- **prod** (default, every channel except the revamp one): the existing
  ``brain_sdk`` SRE brain with the full tool palette and the global
  workspace/worktree roots. ``tool_scope=None`` means "no extra gate" — the
  brain keeps exactly the tools it has today, so prod behaviour is untouched.
- **revamp** (``REVAMP_CHANNEL_ID``): a long-lived workspace for the da-api
  rewrite *project* — it runs the full tool palette (read legacy + write the new
  repo + git + jira + github + notion) and every sub-agent, just like prod, but
  is driven by a project-lead prompt (``brain_revamp``) and is pointed at the
  legacy da-api repo. The current phase ("analysis → docs to Notion first, hold
  tickets/PRs until asked") is held by the **prompt**, not a hard tool gate — the
  user chose prompt-held capability so the channel can grow into ticket/sprint/
  impl/PR work without re-wiring. The hard boundaries that remain are global:
  force-push/reset are denied for every session, and merge/approve still require
  a Slack confirm button.

``tool_scope`` is therefore ``None`` for both tiers today. The scope-gate
machinery in ``permission.py`` stays available for a future phased clamp (e.g.
"unlock jira_create only after docs sign-off") — set a non-None ``tool_scope`` on
a policy to use it.

The policy is resolved from the channel **ID** (the value persisted on the
thread row), not the name, because the brain options factory only has the ID at
session-open time and resolving the name there would cost a Slack API round-trip
on a hot path.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import settings


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


def _revamp_policy() -> WorkspacePolicy:
    roots = tuple(r for r in (settings.revamp_legacy_repo,) if r)
    return WorkspacePolicy(
        name="revamp",
        system_prompt="brain_revamp",
        # Full lifecycle capability — phase discipline is held by the prompt, not
        # a hard gate (see module docstring). subagents=None → all roles available
        # (archaeologist/po/ba for now, dev/review when impl starts).
        tool_scope=None,
        repo_roots=roots,
        subagents=None,
    )


def resolve_policy(channel: str | None) -> WorkspacePolicy:
    """Return the policy for a Slack channel ID. Revamp only when an explicit
    ``REVAMP_CHANNEL_ID`` is configured and matches; everything else is prod."""
    rid = (settings.revamp_channel_id or "").strip()
    if rid and (channel or "").strip() == rid:
        return _revamp_policy()
    return PROD_POLICY
