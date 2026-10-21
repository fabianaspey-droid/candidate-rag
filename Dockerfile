# candidate-rag MCP server: Streamable HTTP, sample data only (Part 3, Step 3).
#
# Build:  docker build -t candidate-rag-mcp:local .
# Run:    docker run --rm -p 8000:8000 --read-only --tmpfs /tmp --memory=1g --cpus=0.25 candidate-rag-mcp:local
#
# What goes in is decided by .dockerignore, which is an allowlist: only the server's own source files, the
# invented sample and requirements-server.txt can enter the build. Real data never reaches it.

# Python 3.13 slim (Debian), pinned by digest so every build starts from exactly the same bytes. This is the
# image Fabian pulled on 20/10/2026 (it printed x86_64 3.13.16). To move to a newer base, change the digest
# on purpose, rebuild and rerun tools/check_container.py.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

# PYTHONDONTWRITEBYTECODE: no .pyc files written at run time (the filesystem is read-only anyway).
# PYTHONUNBUFFERED: log lines reach `docker logs` and CloudWatch straight away, not in blocks.
# ANONYMIZED_TELEMETRY=False: ChromaDB's usage telemetry is on by default; the container calls nothing out.
# MALLOC_ARENA_MAX=2: glibc otherwise gives each worker thread its own memory arena and keeps what it frees.
# Measured 20/10 at 0.25 vCPU / 1 GB with searches already one at a time: 20 queued searches peaked at
# 755 MiB without this setting and 286 MiB with it (the container was OOM-killed at 1 GB before both fixes).
# HOME: Chroma keeps its embedding model under $HOME/.cache. A fixed, root-owned location means the model
# downloaded at build time is the one used at run time, by a user who can read it but not change it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANONYMIZED_TELEMETRY=False \
    MALLOC_ARENA_MAX=2 \
    HOME=/opt/candidate-rag-home

WORKDIR /app

# Dependencies first, in their own layer: a code change then rebuilds in seconds instead of reinstalling.
COPY requirements-server.txt .
RUN pip install --root-user-action=ignore -r requirements-server.txt

COPY src/ src/
COPY sample/ sample/

# At build time, as root: download the embedding model and build the sample index, so the image carries both
# and a new task never depends on an outside download. Then make everything readable but not writable by
# the user the server runs as, and create that user (no home, no login shell).
RUN python src/index.py --sample \
 && rm -rf sample/out \
 && chmod -R a+rX,go-w /app "$HOME" \
 && useradd --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

# Run as that user, by number, so a platform can see it is not root without reading /etc/passwd.
USER 10001

ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
EXPOSE 8000

# No shell in front, so SIGTERM from `docker stop` or ECS reaches Python directly.
CMD ["python", "src/mcp_server.py"]
