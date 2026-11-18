# syntax=docker/dockerfile:1.7

# ---------- build stage: install dependencies into a virtualenv ----------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime stage: slim image, non-root user, only what runs ----------
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="log-sentinel" \
      org.opencontainers.image.description="Predicts system failures from metrics and logs (FastAPI + SQLite)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    SENTINEL_DB=/data/logsentinel.db

RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=app:app logsentinel ./logsentinel

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["python", "-m", "logsentinel", "serve", "--host", "0.0.0.0", "--port", "8000"]
