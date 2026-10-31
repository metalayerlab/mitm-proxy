# mitm-proxy: multi-stage build. Template-owned skeleton; project-specific
# stages (extra binaries, assets) go between the builder and the runtime stage.

# ============================================================
# Stage 1: builder
# ============================================================
FROM python:3.13-slim AS builder

# The `build:` packages in contrib/system-packages.txt (project-owned): compilers
# and headers for dependencies that build from source. None listed: nothing runs
# hadolint ignore=DL3008,SC2046
RUN --mount=type=bind,source=contrib/system-packages.txt,target=/tmp/system-packages.txt \
    set -- $(sed -n 's/^build://p' /tmp/system-packages.txt); \
    if [ $# -gt 0 ]; then \
        apt-get update \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Dependencies, then the package, into /opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

# From the lock file, compiled, without dev dependencies, with the base image's
# Python (never a downloaded one)
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

# Dependencies first, so a source change doesn't reinstall them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable

# ============================================================
# Stage 2: runtime
# ============================================================
FROM python:3.13-slim

# Set by CI (docker-build.yml) from the release; OCI labels for `docker inspect`
ARG VERSION=0.0.0
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="mitm-proxy" \
      org.opencontainers.image.description="Async Python MITM proxy with a self-signed CA — HTTP/1.1 & HTTP/2, pluggable middleware for inspecting and rewriting traffic in flight." \
      org.opencontainers.image.source="https://github.com/metalayerlab/mitm-proxy" \
      org.opencontainers.image.documentation="https://github.com/metalayerlab/mitm-proxy/wiki" \
      org.opencontainers.image.licenses="Unlicense" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# The `runtime:` packages in contrib/system-packages.txt: libraries and tools the
# project needs at run time. None listed: nothing runs
# hadolint ignore=DL3008,SC2046
RUN --mount=type=bind,source=contrib/system-packages.txt,target=/tmp/system-packages.txt \
    set -- $(sed -n 's/^runtime://p' /tmp/system-packages.txt); \
    if [ $# -gt 0 ]; then \
        apt-get update \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --system --create-home --shell /usr/sbin/nologin --uid 1000 mitm-proxy

# Config is mounted at /etc/mitm-proxy (missing is fine: defaults and
# MITM_PROXY_* variables apply); state lives in /var/lib/mitm-proxy
RUN mkdir -p /etc/mitm-proxy /var/lib/mitm-proxy \
    && chown mitm-proxy:mitm-proxy /var/lib/mitm-proxy
COPY contrib/mitm-proxy.example.yaml /usr/share/mitm-proxy/config.example.yaml

# Bind every interface whatever the app's default: in a container, the
# published ports decide what is reachable, and loopback would be unreachable
# The proxy listener too (design 0001): project-side addition to the template line above
ENV MITM_PROXY_LISTEN_HOST=0.0.0.0 \
    MITM_PROXY_PROXY_HOST=0.0.0.0

USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('MITM_PROXY_LISTEN_PORT', '8080')}/healthz\", timeout=4)"]

EXPOSE 8080 3128
VOLUME ["/var/lib/mitm-proxy"]

ENTRYPOINT ["mitm-proxy"]
CMD ["serve"]
