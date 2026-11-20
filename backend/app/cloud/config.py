"""Configuration exclusively for the cloud application."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CloudSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUXIAOYOU_CLOUD_", extra="ignore")

    database_url: str = "postgresql+asyncpg:///suxiaoyou_cloud"
    data_dir: Path = Path("data/cloud")
    model_master_key: SecretStr | None = None
    allowed_origin: str = "http://localhost:3000"
    environment: Literal["production", "development", "test"] = "production"
    cookie_secure: bool = True
    session_hours: int = Field(default=24, ge=1, le=720)
    auto_migrate: bool = False
    allow_mock_provider: bool = False
    full_runtime_enabled: bool = True
    runtime_docker_host: str = "unix:///var/run/docker.sock"
    runtime_image: str = "suxiaoyou-online-runtime:local"
    runtime_gateway_url: str = "http://host.docker.internal:8018/api/cloud/internal/inference/v1"
    runtime_office_url: str = "http://host.docker.internal:8018/api/cloud/internal/office"
    runtime_network_mode: bool = False
    runtime_control_container: str = ""
    runtime_memory: str = "1536m"
    runtime_cpus: float = Field(default=1.0, ge=0.1, le=32)
    runtime_pids: int = Field(default=256, ge=32, le=4096)
    runtime_start_timeout: int = Field(default=120, ge=10, le=600)
    runtime_max_body_bytes: int = Field(default=51 * 1024 * 1024, ge=1024)
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    max_context_chars: int = Field(default=100_000, ge=1)
    default_token_limit: int = Field(default=1_000_000, ge=0)
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: int = Field(default=60, ge=5)
    s3_bucket: str = ""
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    storage_backend: Literal["local", "s3"] = "local"
    login_window_seconds: int = Field(default=900, ge=1)
    login_max_attempts: int = Field(default=10, ge=1)
    login_ip_max_attempts: int = Field(default=100, ge=1)
    captcha_ttl_seconds: int = Field(default=120, ge=30, le=300)
    captcha_window_seconds: int = Field(default=60, ge=1, le=3600)
    captcha_max_attempts: int = Field(default=30, ge=1, le=1000)

    @field_validator("database_url")
    @classmethod
    def async_database_url(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if value.startswith("sqlite://"):
            return value.replace("sqlite://", "sqlite+aiosqlite://", 1)
        if not value.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError("Cloud databases must use PostgreSQL/asyncpg or SQLite/aiosqlite")
        return value

    @field_validator("allowed_origin")
    @classmethod
    def exact_origin(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("allowed_origin must be one exact HTTP(S) origin")
        return value

    @model_validator(mode="after")
    def development_only_migration(self):
        if self.auto_migrate and self.environment == "production":
            raise ValueError("auto_migrate is development/test only; run the migrate CLI in production")
        if self.allow_mock_provider and self.environment == "production":
            raise ValueError("Mock providers are development/test only")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("s3_bucket is required for S3 storage")
        return self
