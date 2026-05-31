"""Configuration loaded from the .env file (see .env.example)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Anthropic / Claude
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 4096
    anthropic_timeout: int = 180

    # PDF rendering
    pdf_dpi: int = 150
    max_pages: int = 25
    image_max_edge: int = 1568     # cap long edge (Anthropic's effective max);
                                   # lower = fewer image tokens, some accuracy risk

    # Cost controls
    enable_prompt_cache: bool = True   # cache the static system prompt (cache_control)

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8001


settings = Settings()
