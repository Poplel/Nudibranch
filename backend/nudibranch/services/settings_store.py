from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import AppSetting


INTEGRATION_KEYS = {
    "acoustid_api_key",
    "jellyfin_url",
    "jellyfin_api_key",
    "slskd_url",
    "slskd_api_key",
}


def integration_settings(session: Session) -> dict[str, str]:
    settings = get_settings()
    values = {
        "acoustid_api_key": settings.acoustid_api_key,
        "jellyfin_url": settings.jellyfin_url,
        "jellyfin_api_key": settings.jellyfin_api_key,
        "slskd_url": settings.slskd_url,
        "slskd_api_key": settings.slskd_api_key,
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
            setting = AppSetting(key=key, value=value or "")
            session.add(setting)
        else:
            setting.value = value or ""
    session.commit()
    return integration_settings(session)


def integration_value(session: Session, key: str) -> str:
    return integration_settings(session).get(key, "")
