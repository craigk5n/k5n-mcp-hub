# Builder stage: install dependencies and build the package
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build tools and upgrade pip
RUN pip install --no-cache-dir --upgrade pip build

# Copy pyproject.toml and install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy the mcp_hub package and install it
COPY src/mcp_hub src/mcp_hub
RUN pip install --no-cache-dir .

# Runtime stage: minimal image with only runtime dependencies
FROM python:3.11-slim

WORKDIR /app

# Copy installed Python packages and the k5n-mcp-hub entrypoint from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/k5n-mcp-hub /usr/local/bin/k5n-mcp-hub

# Copy config.yaml to /app/config.yaml
COPY config.yaml /app/config.yaml

EXPOSE 8080

USER 1000

ENTRYPOINT ["k5n-mcp-hub"]

