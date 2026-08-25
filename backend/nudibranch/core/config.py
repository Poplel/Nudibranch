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

    spotify_client_id: str = Field("", alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field("", alias="SPOTIFY_CLIENT_SECRET")

    apns_enabled: bool = Field(False, alias="APNS_ENABLED")
    # Push is proxy-only. There are no Apple credentials here on purpose: the server never talks to
    # APNS itself, it signs each push with its own Ed25519 identity and hands it to a NudibranchProxy
    # instance, which holds the device tokens and the Apple credentials. That is what lets a
    # self-hoster run push without an Apple developer account. The former direct `.p8` settings
    # (TEAM_ID / KEY_ID / BUNDLE_ID / PRIVATE_KEY_PATH / USE_SANDBOX) are gone; leaving them in a
    # deploy `.env` is harmless and simply ignored.
    apns_proxy_url: str = Field("https://nbpushproxy.pophosting.xyz/", alias="APNS_PROXY_URL")

    import_path: Path = Path("/app/import")
    staging_path: Path = Path("/app/staging")
    library_path: Path = Path("/app/library")
    downloads_path: Path = Path("/app/downloads")
    backups_path: Path = Path("/app/backups")
    # Cover art only. Episode audio is never stored on this server — see `db.models.Podcast`.
    podcasts_path: Path = Path("/app/podcasts")
    config_path: Path = Path("/app/config")
    log_path: Path = Field(Path("/app/config/nudibranch.log"), alias="NUDIBRANCH_LOG_PATH")
    # Optional outbound proxy used ONLY for podcast feed and artwork fetches, e.g.
    # "http://10.0.0.5:8888". Empty = direct. (Episode audio no longer touches this server, so a
    # publisher that refuses it can still be played and downloaded by every client.)
    #
    # ⚠️ Podcast-specific on purpose, not the standard HTTPS_PROXY env var: that would route every
    # outbound request (slskd, Jellyfin, MusicBrainz, the APNS proxy) through the same hop, which
    # is both slower and a privacy change nobody asked for. This exists because a host can refuse
    # the SERVER's network while serving the same URL to a laptop — measured with Patreon, which
    # returns the RSS feed to a datacenter egress but 403s the media endpoints behind it, so the
    # only fix is to send those particular requests out a different way.
    podcast_proxy_url: str = Field("", alias="PODCAST_PROXY_URL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
