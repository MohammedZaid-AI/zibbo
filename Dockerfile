# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------
# Builder — resolve dependencies into a self-contained virtualenv.
# --------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Dependencies change far less often than source. Installing them against a
# stub package keeps this layer cached across ordinary code edits.
COPY pyproject.toml README.md ./
RUN mkdir -p gateway && touch gateway/__init__.py && pip install .

COPY gateway ./gateway
RUN pip install --no-deps .

# tiktoken downloads its BPE files on first use. Doing that here means the runtime
# never reaches for the network — a cold container that cannot resolve the CDN
# would otherwise fall back to approximate token counts on its first request.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN mkdir -p /opt/tiktoken \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"

# --------------------------------------------------------------------------
# Runtime — no build tooling, no source tree, no root.
# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    ZIBBO_HOST=0.0.0.0 \
    ZIBBO_PORT=8000

# libxml2/libxslt: runtime libraries for lxml's C extension.
# ca-certificates: the upstream client verifies TLS against the OS trust store
# (truststore), and on Linux that store *is* /etc/ssl/certs — truststore ships no
# bundle of its own here. Make the dependency explicit so image TLS never silently
# depends on the base image bundling it; an operator can also drop a corporate /
# proxy CA into /usr/local/share/ca-certificates and update-ca-certificates to have
# it trusted, which a certifi-only client would ignore. See _upstream_ssl_context.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 gateway \
    && useradd --system --uid 1001 --gid gateway --no-create-home gateway

COPY --from=builder --chown=root:root /opt/venv /opt/venv
COPY --from=builder --chown=root:root /opt/tiktoken /opt/tiktoken

WORKDIR /app
COPY --chown=root:root gateway ./gateway

USER gateway
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health/live').read()"

# Importing the app runs configure_logging(), which takes ownership of uvicorn's
# loggers. The access log is ours (it carries the request id), so disable theirs.
CMD ["uvicorn", "gateway.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--no-access-log"]
