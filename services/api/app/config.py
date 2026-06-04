"""Runtime configuration sourced entirely from environment variables."""

from __future__ import annotations

import os

FALKOR_HOST: str = os.getenv("FALKOR_HOST", "127.0.0.1")
FALKOR_PORT: int = int(os.getenv("FALKOR_PORT", "6379"))
FALKOR_PASSWORD: str | None = os.getenv("FALKOR_PASSWORD")

BUILD_VERSION: str = os.getenv("CODEGRAPH_VERSION", "0.1.0-dev")

API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
API_PORT: int = int(os.getenv("API_PORT", "8765"))
