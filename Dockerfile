# Builder stage: install dependencies and build the package
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build tools and upgrade pip
RUN pip install --no-cache-dir --upgrade pip build

# Copy pyproject.toml and install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy the devhub package and install it
COPY src/devhub src/devhub
RUN pip install --no-cache-dir .

# Runtime stage: minimal image with only runtime dependencies
FROM python:3.11-slim

WORKDIR /app

# nodejs/npm are included ONLY to support the optional conformance shell-out (~50MB)
RUN apt-get update && \
    apt-get install -y --no-install-recommends nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages and the devhub entrypoint from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/devhub /usr/local/bin/devhub

# Copy config.yaml to /app/config.yaml
COPY config.yaml /app/config.yaml

EXPOSE 8080

USER 1000

ENTRYPOINT ["devhub"]

