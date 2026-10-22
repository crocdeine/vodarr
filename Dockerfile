# VODarr — multi-arch (amd64, arm64). Two stages: dependencies built in a venv, then a slim
# runtime with ffprobe. Runs as PUID/PGID, never as root (ADR-0009).

FROM python:3.12-slim AS builder
WORKDIR /build
RUN python -m venv /venv
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN /venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="VODarr" \
      org.opencontainers.image.description="IPTV VOD downloads rebuilt as their original P2P releases and cross-seeded" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /venv /venv
COPY web /app/web
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENV PATH="/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    VODARR_CONFIG=/config \
    VODARR_WEB=/app/web \
    VODARR_PORT=8863 \
    PUID=1000 \
    PGID=1000
VOLUME ["/config", "/data"]
EXPOSE 8863
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ[\"VODARR_PORT\"]}/api/health', timeout=4)"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# One Uvicorn worker: the download and pipeline threads must exist once (ADR-0002).
CMD ["sh", "-c", "exec uvicorn vodarr.app:create_app --factory --host 0.0.0.0 --port ${VODARR_PORT} --workers 1"]
