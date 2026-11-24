"""signal-broadcast: веб-панель рассылок по группам Signal."""

from __future__ import annotations

from contextlib import asynccontextmanager
from logging.config import dictConfig

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import dispose_engine, init_db
from app.routers import api, pages, pages2
from app.services import broadcaster
from app.services import inbox as inbox_service
from app.services import scheduler as scheduler_service

LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {"level": settings.log_level, "handlers": ["console"]},
}


def apply_logging() -> None:
    dictConfig(LOG_CONFIG)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler_service.start()
    await inbox_service.inbox.start()
    yield
    await broadcaster.stop_all()
    scheduler_service.shutdown()
    await inbox_service.inbox.stop()
    await dispose_engine()


app = FastAPI(title="signal-broadcast", version="0.1.0", lifespan=lifespan)

app.include_router(api.router)
app.include_router(pages.router)
app.include_router(pages2.router)

app.mount("/static", StaticFiles(directory=str(settings.data_dir.parent / "app" / "web" / "static")), name="static")


@app.get("/healthz", response_class=HTMLResponse)
async def healthz() -> HTMLResponse:
    return HTMLResponse("ok", status_code=200)


def main() -> None:
    apply_logging()
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=LOG_CONFIG)


if __name__ == "__main__":
    main()
