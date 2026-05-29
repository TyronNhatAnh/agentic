"""In-process MCP server exposing integration verbs as typed SDK tools.

Phase 0: one sample tool (`github_get_pr`) — enough to prove the @tool +
create_sdk_mcp_server path works end-to-end in the smoke test.

Phase 2: convert all integrations/*.execute_action verbs 1-to-1 to @tool with
typed input_schema. The brain stops parsing JSON and starts calling tools.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..integrations import github as github_int


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


@tool(
    "github_get_pr",
    "Fetch a GitHub pull request's title, body, state, and metadata. "
    "Use when the user mentions a PR number or link.",
    {"pr": int, "repo": str},
)
async def github_get_pr(args: dict[str, Any]) -> dict[str, Any]:
    try:
        text = await github_int.get_pr(int(args["pr"]), repo=args.get("repo") or None)
        return _ok(text)
    except Exception as e:  # surface as tool error, not a Python crash
        return _err(f"github.get_pr failed: {e}")


def build_agentic_mcp_server():
    """Return the in-process MCP server config to pass into ClaudeAgentOptions.

    Phase 0: only `github_get_pr`. Phase 2 expands to the full integration set.
    """
    return create_sdk_mcp_server(
        name="agentic",
        version="0.1.0",
        tools=[github_get_pr],
    )
