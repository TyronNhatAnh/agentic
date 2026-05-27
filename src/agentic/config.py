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
    agent_model: str = Field(default="sonnet", alias="AGENT_MODEL")
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

    workspace_dir: str = Field(default="", alias="WORKSPACE_DIR")
    worktree_dir: str = Field(default="", alias="WORKTREE_DIR")
    services_seed_path: str = Field(default="", alias="AGENTIC_SERVICES_JSON")
    base_branch_template: str = Field(
        default="releases/DAPro-2.{sprint}", alias="BASE_BRANCH_TEMPLATE"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    worker_concurrency: int = Field(default=4, alias="WORKER_CONCURRENCY")

    max_steps: int = Field(default=5, alias="MAX_STEPS")
    max_actions: int = Field(default=5, alias="MAX_ACTIONS")
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

    @property
    def allowed_channel_names(self) -> set[str]:
        return {
            name.strip().lstrip("#").lower()
            for name in self.slack_allowed_channels.split(",")
            if name.strip()
        }


settings = Settings()
