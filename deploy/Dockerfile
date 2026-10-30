# Tracedown server and worker. Dev profile by default: one container, SQLite, no other services.
FROM python:3.12-slim

WORKDIR /app
COPY server/ /app/server/
COPY sdk/python/ /app/sdk/python/
RUN pip install --no-cache-dir "/app/server[enterprise]" /app/sdk/python

ENV TDN_HOST=0.0.0.0 TDN_PORT=7070 TDN_DB=/data/agent_tracedown.db
VOLUME ["/data"]
EXPOSE 7070
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7070/healthz')"
CMD ["tracedown-server"]
