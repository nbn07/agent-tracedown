# The public Tracedown demo: the server, the web app and the demo runs, read-only.
#
#   docker build -t tracedown-demo deploy/demo && docker run -p 7860:7860 tracedown-demo
#
# Hosting steps (Hugging Face Spaces or Render, both free) are in deploy/demo/README.md.
FROM python:3.12-slim
ARG TRACEDOWN_VERSION=0.4.1
RUN pip install --no-cache-dir "agent-tracedown-server==${TRACEDOWN_VERSION}" \
 && useradd --create-home demo
USER demo
WORKDIR /home/demo
ENV PORT=7860
EXPOSE 7860
# A fresh database on every start: the demo never keeps anything a visitor did.
CMD ["sh", "-c", "rm -f demo.db && exec tracedown up --host 0.0.0.0 --port ${PORT} --db demo.db --no-open --read-only"]
