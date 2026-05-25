from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_app_token: str = Field(default="", alias="SLACK_APP_TOKEN")

    claude_bin: str = Field(default="claude", alias="CLAUDE_BIN")
    claude_timeout: int = Field(default=300, alias="CLAUDE_TIMEOUT")

    agentic_db: str = Field(default="agentic.db", alias="AGENTIC_DB")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_default_repo: str = Field(default="", alias="GITHUB_DEFAULT_REPO")
    github_username: str = Field(default="", alias="GITHUB_USERNAME")

    jira_base_url: str = Field(default="", alias="JIRA_BASE_URL")
    jira_email: str = Field(default="", alias="JIRA_EMAIL")
    jira_api_token: str = Field(default="", alias="JIRA_API_TOKEN")
    jira_default_project: str = Field(default="", alias="JIRA_DEFAULT_PROJECT")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


settings = Settings()
