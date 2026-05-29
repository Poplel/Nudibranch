from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import AppSetting


INTEGRATION_KEYS = {
    "jellyfin_url",
    "jellyfin_api_key",
    "jellyfin_user_id",
    "slskd_url",
    "slskd_api_key",
    "slskd_album_match_threshold",
    "slskd_album_folder_tries",
    "slskd_concurrent_downloads",
    "youtube_cookies_browser",
    "youtube_cookies_path",
}


def integration_settings(session: Session) -> dict[str, str]:
    settings = get_settings()
    values = {
        "jellyfin_url": settings.jellyfin_url,
        "jellyfin_api_key": settings.jellyfin_api_key,
        "jellyfin_user_id": "",
        "slskd_url": settings.slskd_url,
        "slskd_api_key": settings.slskd_api_key,
        "slskd_album_match_threshold": "72",
        "slskd_album_folder_tries": "5",
        "slskd_concurrent_downloads": "1",
        "youtube_cookies_browser": "",
        "youtube_cookies_path": str(settings.config_path / "youtube-cookies.txt"),
    }
    for setting in session.query(AppSetting).filter(AppSetting.key.in_(INTEGRATION_KEYS)):
        values[setting.key] = setting.value
    return values


def update_integration_settings(session: Session, values: dict[str, str]) -> dict[str, str]:
    for key, value in values.items():
        if key not in INTEGRATION_KEYS:
            continue
        setting = session.get(AppSetting, key)
        if not setting:
            setting = AppSetting(key=key, value=str(value) if value is not None else "")
            session.add(setting)
        else:
            setting.value = str(value) if value is not None else ""
    session.commit()
    return integration_settings(session)


def integration_value(session: Session, key: str) -> str:
    return integration_settings(session).get(key, "")
