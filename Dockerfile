FROM python:3.12-slim

WORKDIR /app

# System deps (uv is a fast package manager; faster than pip on rebuilds)
RUN pip install --no-cache-dir uv

# Copy project metadata first so Docker layer cache only invalidates
# when dependencies change, not on every code edit.
COPY pyproject.toml README.md ./
COPY analytics_mcp ./analytics_mcp

# `pyarrow` (pulled in by google-adk) is ~42MB and sometimes stalls the
# default 30s HTTP timeout on slow Docker build networks. 5 minutes
# per file is plenty of headroom.
ENV UV_HTTP_TIMEOUT=300

# Install with uv into a system-site install (no venv needed in container).
RUN uv pip install --system --no-cache .

# Default to streamable-http transport in container contexts. Override to
# 'sse' for legacy clients or 'stdio' if this image is ever used behind an
# stdio-bridge.
ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8080

# Entrypoint decodes GOOGLE_APPLICATION_CREDENTIALS_JSON (env) into
# a file and sets GOOGLE_APPLICATION_CREDENTIALS so the google-auth
# library picks it up automatically.
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["analytics-mcp"]
