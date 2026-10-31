# Tracedown server and worker image.
#   server:  tracedown-server          (default command)
#   worker:  tracedown-worker          (Kafka consumers; set TDN_KAFKA_BOOTSTRAP)
# Runs as a non-root user and works with a read-only root filesystem (writes only to /tmp
# and /data). Metadata: TDN_DATABASE_URL (Postgres) or SQLite at TDN_DB.
FROM python:3.12-slim AS build
WORKDIR /src
COPY sdk/python/ sdk/python/
COPY server/ server/
RUN pip install --no-cache-dir --prefix=/install ./sdk/python "./server[enterprise]"

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/nbn07/agent-tracedown" \
      org.opencontainers.image.description="Tracedown server and worker" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /install /usr/local
RUN groupadd --system --gid 10001 tdn && useradd --system --uid 10001 --gid tdn --home-dir /data tdn \
    && mkdir -p /data && chown tdn:tdn /data
ENV TDN_HOST=0.0.0.0 TDN_PORT=7070 TDN_DB=/data/agent_tracedown.db \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 7070
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7070/healthz')"
CMD ["tracedown-server"]
