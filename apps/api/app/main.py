"""FastAPI application entrypoint."""
from __future__ import annotations

# Load `.env` files into os.environ BEFORE any of the app modules import, so
# modules that read `os.environ` directly (the observability build stamp) see the
# same values pydantic-settings sees. pydantic-settings' `env_file=` populates the
# Settings class only, not os.environ. Walks up the directory tree so it picks up
# either apps/api/.env or the repo-root .env, whichever exists.
from pathlib import Path

from dotenv import load_dotenv

for _p in Path(__file__).resolve().parents:
    _env = _p / ".env"
    if _env.exists():
        load_dotenv(_env, override=False)

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .assistants.routes import router as assistants_router
from .business.routes import router as business_router
from .observability import build_info, configure_observability
from .settings import get_settings

logging.basicConfig(level=logging.INFO)

configure_observability()

app = FastAPI(title="Northwind Copilot API", version="0.1.0")
app.include_router(business_router)
app.include_router(assistants_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Correlation-ID", "ETag", "Location"],
)


@app.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "settings_loaded": bool(settings.foundry_endpoint),
            "business_domain": settings.business_domain}


@app.get("/build")
async def build() -> dict:
    """Git SHA and prompt version manifest currently running, so the exact commit
    that shipped every prompt of a turn can be read from a deployed API."""
    return {"build": build_info()}
