from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    app_name: str = Field("Nai Health Monitor", env="APP_NAME")
    host: str = Field("0.0.0.0", env="HOST")
    port: int = Field(5010, env="PORT")

    ready_strategy: str = Field("all", env="READY_STRATEGY")  # all | any

    probe_interval_seconds: int = Field(30, env="PROBE_INTERVAL_SECONDS")
    probe_timeout_seconds: float = Field(5.0, env="PROBE_TIMEOUT_SECONDS")
    probe_concurrency: int = Field(20, env="PROBE_CONCURRENCY")

    history_retention_minutes: int = Field(24 * 60, env="HISTORY_RETENTION_MINUTES")
    history_max_points_per_target: int = Field(3000, env="HISTORY_MAX_POINTS_PER_TARGET")
    availability_windows_minutes: str = Field("60,1440", env="AVAILABILITY_WINDOWS_MINUTES")
    history_db_path: str = Field("./data/history.db", env="HISTORY_DB_PATH")
    db_path: str = Field("", env="DB_PATH")

    expose_urls: bool = Field(False, env="EXPOSE_URLS")
    status_token: str = Field("", env="STATUS_TOKEN")

    auth_enabled: bool = Field(True, env="AUTH_ENABLED")
    auth_username: str = Field("admin", env="AUTH_USERNAME")
    auth_password: str = Field("", env="AUTH_PASSWORD")
    auth_secret_key: str = Field("", env="AUTH_SECRET_KEY")
    auth_cookie_secure: bool = Field(False, env="AUTH_COOKIE_SECURE")
    auth_session_minutes: int = Field(24 * 60, env="AUTH_SESSION_MINUTES")

    keypool_enabled: bool = Field(False, env="KEYPOOL_ENABLED")
    keypool_encryption_key: str = Field("", env="KEYPOOL_ENCRYPTION_KEY")
    keypool_require_opus_tier: bool = Field(False, env="KEYPOOL_REQUIRE_OPUS_TIER")
    keypool_health_check_enabled: bool = Field(True, env="KEYPOOL_HEALTH_CHECK_ENABLED")
    keypool_health_check_interval_seconds: int = Field(300, env="KEYPOOL_HEALTH_CHECK_INTERVAL_SECONDS")
    keypool_health_check_fail_threshold: int = Field(3, env="KEYPOOL_HEALTH_CHECK_FAIL_THRESHOLD")

    # Legacy URL probing targets (unused when monitoring keys only).
    targets: str = Field("", env="TARGETS")


settings = Settings()
