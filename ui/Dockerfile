# syntax=docker/dockerfile:1
#
# ARIA Web UI image.
#
# Nothing about the API is baked in any more: ARIA_API_URL and ARIA_API_KEY are
# read at REQUEST time by the proxy route handler, so changing the URL or
# rotating the key is `docker compose up -d ui`, not a rebuild — and the key
# never reaches the browser bundle (it used to be in eight JS chunks).

FROM node:22-alpine AS builder
WORKDIR /app

COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY . .

# Stamped into the page footer and served at /api/build; `make ui-deploy`
# refuses to finish unless it matches HEAD, so a stale image cannot masquerade
# as the current source (the running image was six days behind when the
# responsive audit ran, and nothing on the page said so).
ARG BUILD_SHA=unknown
ARG BUILD_BRANCH=
ENV BUILD_SHA=$BUILD_SHA BUILD_BRANCH=$BUILD_BRANCH
ENV NEXT_OUTPUT=standalone
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/api/build >/dev/null || exit 1

CMD ["node", "server.js"]
