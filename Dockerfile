# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------- build stage
FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[perf]"

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim-bookworm AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COPYTRADER_CONFIG=/app/config/settings.yaml \
    COPYTRADER_ENV_FILE=/nonexistent
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /app/config /data && chown app:app /data
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY config/settings.example.yaml config/wallets.example.csv config/token_categories.example.yaml ./config/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
USER app
EXPOSE 8080 9464
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"
ENTRYPOINT ["entrypoint.sh"]
CMD ["run"]
