from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from nudibranch import __version__
from nudibranch.api.routes import router
from nudibranch.core.config import get_settings
from nudibranch.db.init import init_db
from nudibranch.db.session import SessionLocal

settings = get_settings()

app = FastAPI(
    title="Nudibranch API",
    version=__version__,
    openapi_url="/api/v1/openapi.json",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    for path in [
        settings.import_path,
        settings.staging_path,
        settings.library_path,
        settings.downloads_path,
        settings.trash_path,
        settings.backups_path,
        settings.config_path,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        init_db(session)


@app.get("/healthz", tags=["system"])
def healthz() -> dict:
    return {"ok": True, "version": __version__}

