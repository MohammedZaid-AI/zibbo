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

# --------------------------------------------------------------------------
# Runtime — no build tooling, no source tree, no root.
# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    LLMGATEWAY_HOST=0.0.0.0 \
    LLMGATEWAY_PORT=8000

RUN groupadd --system --gid 1001 gateway \
    && useradd --system --uid 1001 --gid gateway --no-create-home gateway

COPY --from=builder --chown=root:root /opt/venv /opt/venv

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
