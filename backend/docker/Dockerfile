# ── Stage 1: builder ──────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv (fast Python package installer, replaces pip)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# uv installs into /install prefix — much faster than pip
RUN uv pip install --system --no-cache -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────
FROM python:3.11-slim AS runtime

# Copy uv binary for use in runtime (optional, needed if runtime ever installs pkgs)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Docker CLI — copy the official binary directly from docker:cli image.
# This is the standard approach for sibling-container setups (mounting docker.sock).
# The docker.io apt package on Debian slim is unreliable for CLI-only use.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

# git — needed for cloning repositories via shell MCP
# curl — needed for downloading binaries and healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 — needed for @modelcontextprotocol/server-filesystem (stdio subprocess)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install filesystem MCP server globally via npm
RUN npm install -g @modelcontextprotocol/server-filesystem

# Download GitHub MCP Server (Linux amd64) binary
# Pinned to v1.7.0 — matches the MCP tool schemas used throughout this codebase.
RUN curl -fsSL \
    https://github.com/github/github-mcp-server/releases/download/v1.7.0/github-mcp-server_Linux_x86_64.tar.gz \
    -o /tmp/github-mcp-server.tar.gz && \
    tar -xzf /tmp/github-mcp-server.tar.gz -C /usr/local/bin github-mcp-server && \
    chmod +x /usr/local/bin/github-mcp-server && \
    rm /tmp/github-mcp-server.tar.gz

# Copy Python packages from builder stage
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app

# Copy application source
COPY app/ ./app/
COPY static/ ./static/
COPY scripts/ ./scripts/

# Pre-create sandbox workdir (bind-mounted at runtime via compose volume)
RUN mkdir -p /app/sandbox_workdir

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
