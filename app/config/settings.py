from datetime import timezone, timedelta
from pydantic_settings import BaseSettings
from typing import Dict, Any
import os
import httpx


class Settings(BaseSettings):
    """Application configuration class"""

    # Timezone settings
    TIMEZONE: timezone = timezone(timedelta(hours=8))

    # File path configuration
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    STATIC_DIR: str = os.path.join(BASE_DIR, "static")
    TEMPLATES_DIR: str = os.path.join(BASE_DIR, "templates")

    # Application configuration
    CACHE_TTL: int = 600  # Cache refresh time (seconds)
    DEFAULT_LIMIT: int = 1000000  # Default API quota (1 million tokens)
    MAX_CONNECTIONS: int = 1000  # Max connections
    REQUEST_TIMEOUT: float = 180.0  # Request timeout
    READ_TIMEOUT: float = 300.0  # Read timeout

    # Environment configuration
    ENV: str = os.getenv("ENV", "development")  # Environment: development/production

    # Domain and URL configuration
    DOMAIN: str = os.getenv("DOMAIN", "localhost")
    API_BASE_URL: str = os.getenv("API_BASE_URL", "http://localhost:8087")
    CHAT_URL: str = os.getenv("CHAT_URL", "")
    
    # CORS allowed origins
    @property
    def CORS_ORIGINS(self) -> list:
        """Get the list of allowed CORS origins"""
        origins = [
            self.API_BASE_URL,
            f"https://{self.DOMAIN}",
            f"http://{self.DOMAIN}",
            "http://localhost:8087",
            "http://0.0.0.0:8087",
        ]
        return list(set(origins))  # De-duplicate

    # Session configuration
    SESSION_SECRET_KEY: str = os.getenv(
        "SESSION_SECRET_KEY", "your-secret-key-here"
    )  # Secret key read from environment variable
    SESSION_MAX_AGE: int = 86400  # Session expiry (seconds), 24 hours
    SESSION_COOKIE_SECURE: bool = ENV == "production"  # Use HTTPS only in production
    SESSION_COOKIE_SAMESITE: str = "lax"  # Cookie SameSite policy

    # Admin credentials (read from environment variables for security)
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    # Password hash (bcrypt). Default is empty; must be set in production.
    # Generate with: python -c "import bcrypt; print(bcrypt.hashpw(b'your_password', bcrypt.gensalt()).decode())"
    ADMIN_PASSWORD_HASH: str = os.getenv("ADMIN_PASSWORD_HASH", "")

    # OpenRouter configuration (upstream LLM provider)
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    # Groq configuration (upstream LLM provider)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

    # NVIDIA NIM configuration (upstream LLM provider)
    NVIDIA_NIM_API_KEY: str = os.getenv("NVIDIA_NIM_API_KEY", "")

    @property
    def HTTP_CLIENT_CONFIG(self) -> Dict[str, Any]:
        """HTTP client configuration"""
        return {
            "limits": httpx.Limits(
                max_connections=self.MAX_CONNECTIONS, max_keepalive_connections=100
            ),
            "timeout": httpx.Timeout(
                timeout=self.REQUEST_TIMEOUT, read=self.READ_TIMEOUT
            ),
            "http2": True,
            "transport": httpx.AsyncHTTPTransport(http2=True),
        }

    # Other
    TOKENIZER_MODEL: str = "gpt-3.5-turbo"  # Default tokenizer model
    ANTHROPIC_VERSION: str = "2023-06-01"  # Anthropic API version (adjust as the API evolves)
    HEALTH_CHECK_INTERVAL: int = 60  # Health check interval (seconds)
    MAX_RETRIES: int = 3  # Max HTTP request retries
    MAX_REQUEST_BODY_SIZE: int = 20 * 1024 * 1024  # Max request body size (20MB), protects against malicious payloads

    # API Key cache configuration
    API_KEY_CACHE_TTL: int = 300  # API Key cache TTL (seconds), default 5 minutes
    USAGE_CACHE_TTL: int = 60  # Usage cache TTL (seconds), default 1 minute
    MAX_CACHE_SIZE: int = 10000  # Max cache entries
    PER_REQUEST_RESERVE: float = 5000  # Tokens reserved per concurrent request to prevent race-condition overuse

    class Config:
        case_sensitive = True
        env_file = ".env"
        env_file_encoding = "utf-8"


# Create the global settings instance
settings = Settings()