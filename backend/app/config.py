from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Azure OpenAI ──────────────────────────────────────
    azure_openai_endpoint: str = Field(..., description="Azure OpenAI resource endpoint")
    azure_openai_api_key: str = Field(..., description="Azure OpenAI API key")
    azure_openai_api_version: str = Field(
        default="2024-12-01-preview", description="Azure OpenAI API version"
    )
    azure_openai_deployment: str = Field(
        default="gpt-4.1", description="Azure OpenAI deployment name"
    )

    # ── GitHub ────────────────────────────────────────────
    github_token: str = Field(..., description="GitHub personal access token (repo scope)")

    # ── Langfuse ──────────────────────────────────────────
    langfuse_public_key: str = Field(..., description="Langfuse public key")
    langfuse_secret_key: str = Field(..., description="Langfuse secret key")
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", description="Langfuse host URL"
    )

    # ── Sandbox ───────────────────────────────────────────
    sandbox_workdir: str = Field(
        default="./sandbox_workdir",
        description="Container-side path for per-run repo clones",
    )
    sandbox_workdir_host: str = Field(
        default="",
        description=(
            "HOST-side absolute path to sandbox_workdir. "
            "Required when running in Docker so that `docker run -v <path>:/workspace` "
            "uses the host path the Docker daemon can resolve. "
            "Leave empty when running locally (host path == workdir)."
        ),
    )
    sandbox_image: str = Field(
        default="repopilot-sandbox",
        description="Docker image used for sandboxed test execution",
    )

    # ── Fixer ─────────────────────────────────────────────
    max_fixer_attempts: int = Field(
        default=3, description="Maximum Fixer retry attempts before escalation"
    )

    # ── App ───────────────────────────────────────────────
    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")


# Singleton — imported everywhere
settings = Settings()
