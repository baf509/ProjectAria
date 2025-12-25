"""
ARIA - Configuration

Phase: 1
Purpose: Application settings using pydantic-settings

Related Spec Sections:
- Section 10.2: Pydantic Settings
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # MongoDB (8.2 with replica set)
    mongodb_uri: str = "mongodb://localhost:27017/?directConnection=true&replicaSet=rs0"
    mongodb_database: str = "aria"

    # Ollama
    ollama_url: str = "http://localhost:11434"

    # Cloud LLMs
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openrouter_api_key: str = ""

    # Embeddings
    embedding_provider: str = "ollama"
    embedding_ollama_model: str = "qwen3-embedding:0.6b"
    embedding_dimension: int = 1024
    voyage_api_key: str = ""

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


settings = Settings()
