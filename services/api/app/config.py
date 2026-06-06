"""Runtime configuration sourced entirely from environment variables."""

from __future__ import annotations

import os

FALKOR_HOST: str = os.getenv("FALKOR_HOST", "127.0.0.1")
FALKOR_PORT: int = int(os.getenv("FALKOR_PORT", "6379"))
FALKOR_PASSWORD: str | None = os.getenv("FALKOR_PASSWORD")

BUILD_VERSION: str = os.getenv("CODEGRAPH_VERSION", "0.1.0-dev")

API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
API_PORT: int = int(os.getenv("API_PORT", "8765"))

# Host-to-container path translation for bind-mounted repos volume.
# REPOS_ROOT_HOST: the host-side directory mounted into the container (e.g. D:\SanJose).
# REPOS_ROOT_CONTAINER: where it appears inside the container (default /repos).
# When REPOS_ROOT_HOST is empty the backend is assumed to be running natively and no
# translation is applied.
REPOS_ROOT_HOST: str = os.getenv("REPOS_ROOT_HOST", "")
REPOS_ROOT_CONTAINER: str = os.getenv("REPOS_ROOT_CONTAINER", "")
