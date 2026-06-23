"""CodeGraph API daemon — FastAPI application entry point."""

from __future__ import annotations

import logging
import logging.config
import pathlib
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse

from app.config import BUILD_VERSION
from app.falkor import FalkorClient
from app.models import HealthResponse, VersionResponse
from app.routers import graphs, repositories

# ---------------------------------------------------------------------------
# Logging — structured format. ExtraFieldFormatter appends any extra={...}
# context (service, language, exit_code, stderr_tail, ...) to each line so the
# diagnostics we attach are actually rendered on the console.
# ---------------------------------------------------------------------------

logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structured": {
                "()": "app.logging_utils.ExtraFieldFormatter",
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "structured",
            }
        },
        "root": {"handlers": ["console"], "level": "INFO"},
    }
)

logger = logging.getLogger(__name__)
_SERVICE = "api"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(
        "CodeGraph API starting",
        extra={"service": _SERVICE, "version": BUILD_VERSION},
    )
    app.state.falkor = FalkorClient()
    yield
    logger.info("CodeGraph API stopped", extra={"service": _SERVICE})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CodeGraph API",
    version=BUILD_VERSION,
    description="Local REST daemon for the CodeGraph desktop tool.",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def timing_middleware(request: Request, call_next: Callable) -> Response:
    start = time.perf_counter()
    response: Response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        "HTTP request",
        extra={
            "service": _SERVICE,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


app.include_router(graphs.router, prefix="/api/v1")
app.include_router(repositories.router, prefix="/api/v1")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

_UI_HTML = pathlib.Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui() -> HTMLResponse:
    return HTMLResponse(_UI_HTML.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# System endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health(request: Request) -> HealthResponse:
    falkor: FalkorClient = request.app.state.falkor
    falkordb_status = "ok" if falkor.ping() else "unreachable"
    overall = "ok" if falkordb_status == "ok" else "degraded"
    return HealthResponse(status=overall, falkordb=falkordb_status)


@app.get("/api/v1/version", response_model=VersionResponse, tags=["system"])
async def version() -> VersionResponse:
    return VersionResponse(version=BUILD_VERSION)
