# Tracedown web app (Next.js standalone server on port 3000).
# Build from the repository root:  docker build -f deploy/web.Dockerfile -t tracedown-web .
# The app proxies /api, /v1 and /auth to the API server at TDN_API_URL.
FROM node:22-alpine AS deps
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-alpine AS build
WORKDIR /app
ARG TDN_API_URL=http://tracedown-server:7070
ENV NEXT_TELEMETRY_DISABLED=1 TDN_API_URL=${TDN_API_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./
RUN mkdir -p public && npm run build

FROM node:22-alpine
LABEL org.opencontainers.image.source="https://github.com/nbn07/agent-tracedown" \
      org.opencontainers.image.description="Tracedown web app" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER 10001
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1
CMD ["node", "server.js"]
