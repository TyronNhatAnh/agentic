from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_app_token: str = Field(default="", alias="SLACK_APP_TOKEN")

    claude_bin: str = Field(default="claude", alias="CLAUDE_BIN")
    claude_timeout: int = Field(default=300, alias="CLAUDE_TIMEOUT")
    # Explicit ids, not the `opus` alias: the alias tracks the installed CLI's
    # family version, so a CLI upgrade would swap the model under us.
    brain_model: str = Field(default="claude-opus-5", alias="BRAIN_MODEL")
    dev_model: str = Field(default="claude-opus-5", alias="DEV_MODEL")
    agent_model: str = Field(default="claude-opus-5", alias="AGENT_MODEL")
    # low|medium|high|xhigh|max; empty = the CLI's own default, which is
    # undocumented and shifts on upgrade (same trap as the model alias above).
    brain_effort: str = Field(default="medium", alias="BRAIN_EFFORT")
    claude_runtime_dir: str = Field(
        default="/tmp/agentic-runtime", alias="CLAUDE_RUNTIME_DIR"
    )

    agentic_db: str = Field(default="agentic.db", alias="AGENTIC_DB")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_default_repo: str = Field(default="", alias="GITHUB_DEFAULT_REPO")
    github_username: str = Field(default="", alias="GITHUB_USERNAME")

    grafana_stag_base_url: str = Field(default="", alias="GRAFANA_STAG_BASE_URL")
    grafana_stag_loki_uid: str = Field(default="", alias="GRAFANA_STAG_LOKI_UID")
    grafana_prod_base_url: str = Field(default="", alias="GRAFANA_PROD_BASE_URL")
    grafana_prod_loki_uid: str = Field(default="", alias="GRAFANA_PROD_LOKI_UID")
    # Service-account basic-auth credential (devops-issued) — the sole Grafana auth.
    # One credential works on BOTH the nonprod and prod-kr instances. User defaults
    # to the SA name; only the password lives in GRAFANA_SA_KR.
    grafana_sa_kr: str = Field(default="", alias="GRAFANA_SA_KR")
    grafana_sa_user: str = Field(default="grafana-sa-kr", alias="GRAFANA_SA_USER")

    # Path to the jlog.sh wrapper (java_logs). Empty = the default skill-dir path.
    # Needs to be a Settings field, not just os.environ: nothing loads `.env` into
    # the process env, so an env_file-only knob would silently do nothing.
    jlog_script: str = Field(default="", alias="JLOG_SCRIPT")

    jira_base_url: str = Field(default="", alias="JIRA_BASE_URL")
    jira_email: str = Field(default="", alias="JIRA_EMAIL")
    jira_api_token: str = Field(default="", alias="JIRA_API_TOKEN")
    jira_default_project: str = Field(default="", alias="JIRA_DEFAULT_PROJECT")
    jira_board_id: int = Field(default=0, alias="JIRA_BOARD_ID")

    # Notion. Empty token disables the notion_create_page tool (it returns a CONFIG
    # error instead of failing). NOTION_PAGE_ID is the default parent page new pages
    # nest under when the caller doesn't pass an explicit parent.
    notion_token: str = Field(default="", alias="NOTION_TOKEN")
    notion_parent_page_id: str = Field(default="", alias="NOTION_PAGE_ID")
    notion_version: str = Field(default="2022-06-28", alias="NOTION_VERSION")

    # db_query goes through ggx-kr-order-service's debug-query admin API (the bot
    # host can't reach the staging DB directly — VPN). That endpoint only exists
    # when APP_ENV != prod, so this pair can't hit production. Empty = tool off.
    order_debug_base_url: str = Field(default="", alias="ORDER_DEBUG_BASE_URL")
    order_debug_admin_token: str = Field(default="", alias="ORDER_DEBUG_ADMIN_TOKEN")
    # db_query_prod: same endpoint on the prod host, backed by a @@read_only=1
    # replica. Runs inline — the server-side audit log is the control, not a button.
    order_debug_prod_base_url: str = Field(default="", alias="ORDER_DEBUG_PROD_BASE_URL")
    order_debug_prod_admin_token: str = Field(default="", alias="ORDER_DEBUG_PROD_ADMIN_TOKEN")
    # Auto-login fallback when ORDER_DEBUG_PROD_ADMIN_TOKEN is empty: POST the admin
    # manual-login form for a cookie token, cached in-process, re-issued on 401/403.
    # The login host must match the DB env's gateway — a staging token won't auth prod.
    order_debug_prod_login_url: str = Field(default="", alias="ORDER_DEBUG_PROD_BASE_URL_LOGIN")
    order_debug_prod_email: str = Field(default="", alias="ORDER_DEBUG_PROD_BASE_EMAIL")
    order_debug_prod_pass: str = Field(default="", alias="ORDER_DEBUG_PROD_BASE_PASS")
    # The server caps at 1000 rows / 15s itself; these are client-side bounds so a
    # broad query can't bloat the transcript and the HTTP wait stays above the
    # server's query timeout.
    order_debug_row_cap: int = Field(default=200, alias="ORDER_DEBUG_ROW_CAP")
    order_debug_timeout_s: int = Field(default=20, alias="ORDER_DEBUG_TIMEOUT_S")

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

    # Per-turn circuit breakers. brain_timeout_s is a wall-clock deadline (on expiry
    # the pooled client is discarded — half-consumed stream). The two SDK-native caps
    # ship 0 = off: they cut deep investigations mid-answer, so today only a *stalled*
    # turn is bounded, not an expensive one. Both are best-effort/between-turns anyway.
    brain_timeout_s: int = Field(default=1200, alias="BRAIN_TIMEOUT_S")
    brain_max_turns: int = Field(default=0, alias="BRAIN_MAX_TURNS")
    brain_max_budget_usd: float = Field(default=0.0, alias="BRAIN_MAX_BUDGET_USD")

    slack_allowed_channels: str = Field(default="", alias="SLACK_ALLOWED_CHANNELS")

    # Hourly server-health monitor (monitor.py). Needs BOTH enabled=true and a
    # channel, else start_monitor no-ops with a warning rather than breaking startup.
    monitor_enabled: bool = Field(default=False, alias="MONITOR_ENABLED")
    monitor_channel: str = Field(default="", alias="MONITOR_CHANNEL")
    monitor_interval_s: int = Field(default=3600, alias="MONITOR_INTERVAL_S")
    monitor_env: str = Field(default="prod", alias="MONITOR_ENV")
    monitor_window: str = Field(default="1h", alias="MONITOR_WINDOW")
    # Empty = every registered service that has a loki_selector; else a CSV of
    # service names/aliases to narrow the watch list.
    monitor_services: str = Field(default="", alias="MONITOR_SERVICES")
    # 5xx/crash count over the window to alert on. Low because the filter now counts
    # real server errors (HTTP 5xx + fatal/panic), not "error"-substring noise.
    monitor_error_threshold: int = Field(default=5, alias="MONITOR_ERROR_THRESHOLD")
    # CSV of `name=url` (or bare `url`) HTTP endpoints to GET each cycle.
    monitor_health_urls: str = Field(default="", alias="MONITOR_HEALTH_URLS")
    monitor_always_post: bool = Field(default=False, alias="MONITOR_ALWAYS_POST")

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
