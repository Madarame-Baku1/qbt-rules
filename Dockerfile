# qbt-rules Dockerfile - Multi-stage build
# Supports both SQLite (default) and Redis queue backends

# ============================================================
# Stage 1: Builder
# ============================================================
FROM python:3.11-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy package files
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install dependencies and package (proper install, not editable)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir . && \
    pip install --no-cache-dir redis  # Include optional Redis support

# ============================================================
# Stage 2: Runtime
# ============================================================
FROM python:3.11-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy default config files to /usr/share (FHS standard location)
RUN mkdir -p /usr/share/qbt-rules
COPY config/config.default.yml /usr/share/qbt-rules/
COPY config/rules.default.yml /usr/share/qbt-rules/

# Create non-root user
RUN useradd --create-home --shell /bin/bash --uid 1000 qbtrules && \
    mkdir -p /config && \
    chown -R qbtrules:qbtrules /config

# Set working directory
WORKDIR /app

# Starts as root only to give /config to PUID:PGID (default 1000:1000,
# the qbtrules user), then drops to that user -- see docker/qbt-rules.sh.
# The wrapper shadows the real qbt-rules on PATH so `docker exec` runs as
# the same user as the server.
COPY docker/qbt-rules.sh /usr/local/bin/qbt-rules
RUN chmod 0755 /usr/local/bin/qbt-rules
ENV PATH="/usr/local/bin:$PATH"

# Default configuration directory
ENV CONFIG_DIR=/config

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000/api/health').raise_for_status()" || exit 1

# Expose server port
EXPOSE 5000

# Volume for configuration and database
VOLUME ["/config"]

# Default command: run server
ENTRYPOINT ["qbt-rules"]
CMD ["--serve"]
