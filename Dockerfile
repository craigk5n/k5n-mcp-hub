# syntax=docker/dockerfile:1

# ---- Builder: install the package + its declared dependencies ----
FROM python:3.11-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

# Copy metadata + source together and install once. (Separate dep/source layers aren't
# worth it here since deps are pinned in pyproject and the source is small.)
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# ---- Runtime: minimal image with only what's needed to run ----
FROM python:3.11-slim

LABEL org.opencontainers.image.title="k5n-mcp-hub" \
      org.opencontainers.image.description="Registry, reverse proxy, and dev/observability hub for MCP servers" \
      org.opencontainers.image.version="0.1.0"

WORKDIR /app

# Bring over the installed site-packages and the console entry point from the builder.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/k5n-mcp-hub /usr/local/bin/k5n-mcp-hub

# Ship the default config (mirrors the built-in defaults); mount over it to customize.
COPY config.yaml /app/config.yaml

# Bind all interfaces inside the container so a published port (docker run -p) is reachable.
# The app default is 127.0.0.1 (correct for a local install); override this env to change it.
ENV MCPHUB_SERVER__HTTP_HOST=0.0.0.0

# The app writes its agent-fixture store under the working dir at runtime; make /app writable
# by the non-root user so that (and any mounted config) works without running as root.
RUN mkdir -p /app/.mcp_hub && chown -R 1000:1000 /app

EXPOSE 8080

USER 1000

# Liveness against the built-in /healthz endpoint (checks the in-container default port).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status == 200 else 1)"

ENTRYPOINT ["k5n-mcp-hub"]
