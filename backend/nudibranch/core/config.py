from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field("development", alias="NUDIBRANCH_ENV")
    public_url: str = Field("http://localhost:5173", alias="NUDIBRANCH_PUBLIC_URL")
    api_url: str = Field("http://api:8000", alias="NUDIBRANCH_API_URL")
    db_path: Path = Field(Path("/app/config/nudibranch.sqlite"), alias="NUDIBRANCH_DB_PATH")
    first_admin_pin: str = Field("123456", alias="NUDIBRANCH_FIRST_ADMIN_PIN")
    full_access_api_key: str = Field("change-me-before-exposing", alias="NUDIBRANCH_FULL_ACCESS_API_KEY")

    jellyfin_url: str = Field("http://jellyfin:8096", alias="JELLYFIN_URL")
    jellyfin_api_key: str = Field("", alias="JELLYFIN_API_KEY")
    slskd_url: str = Field("http://slskd:5030", alias="SLSKD_URL")
    slskd_api_key: str = Field("", alias="SLSKD_API_KEY")
    acoustid_api_key: str = Field("", alias="ACOUSTID_API_KEY")

    trash_retention_days: int = Field(30, alias="TRASH_RETENTION_DAYS")

    spotify_client_id: str = Field("", alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field("", alias="SPOTIFY_CLIENT_SECRET")

    apns_enabled: bool = Field(False, alias="APNS_ENABLED")
    apns_use_sandbox: bool = Field(True, alias="APNS_USE_SANDBOX")
    apns_team_id: str = Field("", alias="APNS_TEAM_ID")
    apns_key_id: str = Field("", alias="APNS_KEY_ID")
    apns_bundle_id: str = Field("", alias="APNS_BUNDLE_ID")
    apns_private_key_path: Path = Field(Path("/app/config/AuthKey.p8"), alias="APNS_PRIVATE_KEY_PATH")

    # Proxy mode: point at a NudibranchProxy server instead of calling Apple directly.
    # When set, direct APNS credentials above are not required.
    apns_proxy_url: str = Field("", alias="APNS_PROXY_URL")

    import_path: Path = Path("/app/import")
    staging_path: Path = Path("/app/staging")
    library_path: Path = Path("/app/library")
    downloads_path: Path = Path("/app/downloads")
    trash_path: Path = Path("/app/trash")
    backups_path: Path = Path("/app/backups")
    config_path: Path = Path("/app/config")
    log_path: Path = Field(Path("/app/config/nudibranch.log"), alias="NUDIBRANCH_LOG_PATH")


@lru_cache
def get_settings() -> Settings:
    return Settings()
