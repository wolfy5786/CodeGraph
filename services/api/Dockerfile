FROM python:3.12-slim

WORKDIR /srv

# ---------------------------------------------------------------------------
# SCIP indexer toolchains (Python + Java)
#
# The ingestion worker spawns indexers by bare name (`scip-python`,
# `scip-java`), so each must resolve on PATH inside this image. These layers
# are placed before the application copy so they stay cached across app/code
# changes. (Built for linux/amd64 — the Coursier launcher below is x86_64.)
# ---------------------------------------------------------------------------

# --- Java toolchain: JDK + Coursier + scip-java -----------------------------
# scip-java is distributed via Coursier and needs a JDK at both install and
# index time. Coursier installs launchers into /root/.local/share/coursier/bin.
ENV PATH="/root/.local/share/coursier/bin:${PATH}"
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jdk curl ca-certificates gzip \
 && curl -fL "https://github.com/coursier/launchers/raw/master/cs-x86_64-pc-linux.gz" \
      | gzip -d > /usr/local/bin/cs \
 && chmod +x /usr/local/bin/cs \
 && cs install --contrib scip-java \
 && command -v scip-java \
 && rm -rf /var/lib/apt/lists/*

# --- Node toolchain: Node.js 20 LTS + scip-python ---------------------------
# scip-python is an npm package; Node 20 LTS (via NodeSource) bundles npm.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @sourcegraph/scip-python \
 && npm cache clean --force \
 && scip-python --version \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/api/app/ ./app/
COPY services/ingestion-worker/src/ ./ingestion-worker/src/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
