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
    anthropic_temperature: float = 0.0   # 0 = deterministic + most accurate for extraction

    # PDF rendering
    pdf_dpi: int = 150
    max_pages: int = 25
    image_max_edge: int = 1568     # cap long edge (Anthropic's effective max);
                                   # lower = fewer image tokens, some accuracy risk

    # Cost controls
    enable_prompt_cache: bool = True   # cache the static system prompt (cache_control)

    # Local LLM (Ollama) — used by /extract-workorder-with-local-llm
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen2.5:14b"
    ollama_timeout: int = 900          # CPU inference is slow; allow minutes

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8001

    # Auth — comma-separated list of accepted API keys for /extract-grouped.
    # Callers must send one as the `X-API-Key` header. Empty = auth DISABLED.
    api_auth_keys: str = ""

    @property
    def auth_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_auth_keys.split(",") if k.strip()}


settings = Settings()
