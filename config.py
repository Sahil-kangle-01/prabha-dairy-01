"""
config.py

Production configuration with environment validation and security settings.
"""

import os
from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables or .env file.

    All required variables must be set or the application will fail to start.
    """

    # Database
    database_url: str = Field(
        ...,
        description="PostgreSQL connection string: postgresql+psycopg2://user:pass@host:5432/dbname"
    )

    # Tally ERP Connection
    tally_host: str = Field(default="127.0.0.1", description="Tally ERP host")
    tally_port: int = Field(default=9000, description="Tally ERP port")

    # API Security
    api_key: Optional[str] = Field(
        default=None,
        description="API authentication key. If set, all requests must include 'X-API-Key' header"
    )
    allowed_origins: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        description="Comma-separated CORS allowed origins"
    )

    # Server Configuration
    environment: str = Field(default="production", description="Environment: development, staging, production")
    debug: bool = Field(default=False, description="Enable debug mode (never in production)")
    host: str = Field(default="0.0.0.0", description="Server bind host")
    port: int = Field(default=8000, description="Server bind port")
    workers: int = Field(default=4, description="Number of worker processes")

    # Database Pool Settings
    db_pool_size: int = Field(default=10, description="SQLAlchemy connection pool size")
    db_max_overflow: int = Field(default=20, description="SQLAlchemy max overflow connections")
    db_pool_timeout: int = Field(default=30, description="Pool checkout timeout in seconds")
    db_pool_recycle: int = Field(default=3600, description="Connection recycle time in seconds")

    # Rate Limiting
    rate_limit_enabled: bool = Field(default=True, description="Enable API rate limiting")
    rate_limit_requests: int = Field(default=100, description="Max requests per window")
    rate_limit_window: int = Field(default=60, description="Rate limit window in seconds")

    # Logging
    log_level: str = Field(default="INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR")

    # Session & Security
    secret_key: str = Field(
        default="CHANGE_ME_IN_PRODUCTION",
        description="Secret key for sessions (generate with: openssl rand -hex 32)"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
        # Check that there's actually a password between "://" and "@"
        # Format: postgresql+psycopg2://user:password@host:port/db
        if "@" in v:
            userinfo = v.split("://", 1)[-1].split("@", 1)[0]
            if ":" not in userinfo or not userinfo.split(":", 1)[1]:
                raise ValueError("DATABASE_URL appears to be missing a password")
        return v

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = ["development", "staging", "production"]
        if v.lower() not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {allowed}")
        return v.lower()

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if v == "CHANGE_ME_IN_PRODUCTION":
            import logging
            logging.warning(
                "SECRET_KEY is using default value! "
                "Generate a secure key with: openssl rand -hex 32"
            )
            # Allow the default to pass so the server can start in dev;
            # production deployments should set a real key in .env.
            return v
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def cors_origins(self) -> list[str]:
        """Parse allowed origins from comma-separated string"""
        if self.is_development:
            return ["*"]
        return [origin.strip() for origin in self.allowed_origins.split(",")]


# Global settings instance
settings = Settings()


def validate_environment():
    """
    Run startup validation checks and log configuration.
    """
    import logging
    logger = logging.getLogger(__name__)

    logger.info("=" * 70)
    logger.info("PRABHA DAIRY - PRODUCTION CONFIGURATION")
    logger.info("=" * 70)
    logger.info(f"Environment      : {settings.environment.upper()}")
    logger.info(f"Debug Mode       : {settings.debug}")
    logger.info(f"Host:Port        : {settings.host}:{settings.port}")
    logger.info(f"Workers          : {settings.workers}")
    logger.info(f"Database         : {settings.database_url.split('@')[1] if '@' in settings.database_url else 'configured'}")
    logger.info(f"Tally ERP        : {settings.tally_host}:{settings.tally_port}")
    logger.info(f"API Key Auth     : {'Enabled' if settings.api_key else 'Disabled'}")
    logger.info(f"Rate Limiting    : {'Enabled' if settings.rate_limit_enabled else 'Disabled'}")
    logger.info(f"CORS Origins     : {', '.join(settings.cors_origins[:3])}{'...' if len(settings.cors_origins) > 3 else ''}")
    logger.info("=" * 70)

    # Production safety checks
    if settings.is_production:
        issues = []

        if settings.debug:
            issues.append("[WARNING] Debug mode is enabled in production")

        if not settings.api_key:
            issues.append("[WARNING] No API key configured - authentication disabled")

        if settings.secret_key == "CHANGE_ME_IN_PRODUCTION":
            issues.append("[WARNING] Using default SECRET_KEY")

        if "*" in settings.cors_origins:
            issues.append("[WARNING] CORS is wide open (allows all origins)")

        if issues:
            logger.warning("PRODUCTION SECURITY WARNINGS:")
            for issue in issues:
                logger.warning(f"  {issue}")
            logger.warning("Fix these before deploying to production!")

    logger.info("Configuration validated successfully")
