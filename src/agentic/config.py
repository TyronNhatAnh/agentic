from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_app_token: str = Field(default="", alias="SLACK_APP_TOKEN")

    claude_bin: str = Field(default="claude", alias="CLAUDE_BIN")
    claude_timeout: int = Field(default=300, alias="CLAUDE_TIMEOUT")
    # Models pinned per role so behavior is deterministic instead of inheriting
    # whatever the host `claude` CLI defaults to. Aliases (opus/sonnet/haiku) are
    # resolved by the CLI to its current version of each family.
    brain_model: str = Field(default="opus", alias="BRAIN_MODEL")
    dev_model: str = Field(default="opus", alias="DEV_MODEL")
    agent_model: str = Field(default="opus", alias="AGENT_MODEL")
    claude_runtime_dir: str = Field(
        default="/tmp/agentic-runtime", alias="CLAUDE_RUNTIME_DIR"
    )

    agentic_db: str = Field(default="agentic.db", alias="AGENTIC_DB")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_default_repo: str = Field(default="", alias="GITHUB_DEFAULT_REPO")
    github_username: str = Field(default="", alias="GITHUB_USERNAME")

    grafana_api_key_stag: str = Field(default="", alias="GRAFANA_API_KEY_STAG")
    grafana_stag_base_url: str = Field(default="", alias="GRAFANA_STAG_BASE_URL")
    grafana_stag_loki_uid: str = Field(default="", alias="GRAFANA_STAG_LOKI_UID")
    grafana_api_key_prod: str = Field(default="", alias="GRAFANA_API_KEY_PROD")
    grafana_prod_base_url: str = Field(default="", alias="GRAFANA_PROD_BASE_URL")
    grafana_prod_loki_uid: str = Field(default="", alias="GRAFANA_PROD_LOKI_UID")

    jira_base_url: str = Field(default="", alias="JIRA_BASE_URL")
    jira_email: str = Field(default="", alias="JIRA_EMAIL")
    jira_api_token: str = Field(default="", alias="JIRA_API_TOKEN")
    jira_default_project: str = Field(default="", alias="JIRA_DEFAULT_PROJECT")
    jira_board_id: int = Field(default=0, alias="JIRA_BOARD_ID")

    # Notion (revamp docs sink). Empty token disables the notion_create_page tool
    # and the revamp pipeline's Notion writes (pipeline reports the missing config
    # instead of failing mid-run).
    notion_token: str = Field(default="", alias="NOTION_TOKEN")
    # Reuses the existing NOTION_PAGE_ID from .env.example as the parent page the
    # revamp pipeline nests module/spec pages under.
    notion_parent_page_id: str = Field(default="", alias="NOTION_PAGE_ID")
    notion_version: str = Field(default="2022-06-28", alias="NOTION_VERSION")

    # da-api revamp tier. The bot tells prod-ops apart from revamp by Slack channel
    # ID (resolve_policy in policy.py). Legacy repo is the read-only Ruby source the
    # archaeologist analyses; it must be a path the SDK can read (added to add_dirs).
    revamp_channel_id: str = Field(default="", alias="REVAMP_CHANNEL_ID")
    revamp_legacy_repo: str = Field(default="", alias="REVAMP_LEGACY_REPO")
    # Service name (in the registry) of the rewrite target repo for the revamp
    # project — where impl/PR work will land once that phase starts. Recorded now
    # so the binding is explicit; hard per-repo scope enforcement is a later phase.
    revamp_target_service: str = Field(default="", alias="REVAMP_TARGET_SERVICE")
    # Hard cap on modules analysed per `revamp <scope>` run — a backstop so a broad
    # scope can't fan out into hundreds of archaeologist calls. The pipeline logs
    # when it truncates rather than silently dropping modules.
    revamp_module_cap: int = Field(default=40, alias="REVAMP_MODULE_CAP")

    workspace_dir: str = Field(default="", alias="WORKSPACE_DIR")
    worktree_dir: str = Field(default="", alias="WORKTREE_DIR")
    services_seed_path: str = Field(default="", alias="AGENTIC_SERVICES_JSON")
    base_branch_template: str = Field(
        default="releases/DAPro-2.{sprint}", alias="BASE_BRANCH_TEMPLATE"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    worker_concurrency: int = Field(default=4, alias="WORKER_CONCURRENCY")

    max_steps: int = Field(default=20, alias="MAX_STEPS")
    max_actions: int = Field(default=20, alias="MAX_ACTIONS")
    brain_max_iterations: int = Field(default=4, alias="BRAIN_MAX_ITERATIONS")
    max_input_chars: int = Field(default=16000, alias="MAX_INPUT_CHARS")
    max_context_chars: int = Field(default=8000, alias="MAX_CONTEXT_CHARS")
    dev_context_chars: int = Field(default=16000, alias="DEV_CONTEXT_CHARS")
    # Brain thread-history budget. Defaults are generous so an earlier in-thread
    # analysis the bot itself produced survives into the brain's view instead of
    # being truncated before it can be reused as a fix spec.
    brain_history_budget_chars: int = Field(
        default=60000, alias="BRAIN_HISTORY_BUDGET_CHARS"
    )
    brain_history_msg_cap_chars: int = Field(
        default=12000, alias="BRAIN_HISTORY_MSG_CAP_CHARS"
    )

    slack_allowed_channels: str = Field(default="", alias="SLACK_ALLOWED_CHANNELS")

    min_claude_version: str = Field(default="2.0.0", alias="MIN_CLAUDE_VERSION")
    sdk_session_idle_ttl_s: int = Field(default=1800, alias="SDK_SESSION_IDLE_TTL_S")
    sdk_max_concurrent_sessions: int = Field(default=20, alias="SDK_MAX_CONCURRENT_SESSIONS")

    @property
    def allowed_channel_names(self) -> set[str]:
        """Returns lowercase names and raw IDs (e.g. D0XXXXXX) from SLACK_ALLOWED_CHANNELS."""
        result = set()
        for entry in self.slack_allowed_channels.split(","):
            entry = entry.strip()
            if not entry:
                continue
            # Channel IDs (C.../D.../G...) are kept as-is (uppercase); names are lowercased
            if entry and entry[0].isupper() and entry.isalnum():
                result.add(entry)
            else:
                result.add(entry.lstrip("#").lower())
        return result


settings = Settings()
