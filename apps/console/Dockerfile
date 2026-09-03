# syntax=docker/dockerfile:1
#
# Multi-stage so the runtime image ships next's standalone output only --
# not the workspace, not devDependencies, not the pnpm store (SECURITY §4).

FROM node:22-slim AS base
RUN corepack enable

FROM base AS deps
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

FROM base AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM base AS runtime

RUN groupadd --system console && \
    useradd --system --gid console --no-create-home --shell /usr/sbin/nologin console

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

COPY --from=builder --chown=console:console /app/public ./public
COPY --from=builder --chown=console:console /app/.next/standalone ./
COPY --from=builder --chown=console:console /app/.next/static ./.next/static

USER console

EXPOSE 3000

# node, not curl/wget -- same reasoning as the api image: no package
# installed for nothing but this check.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["node", "-e", "require('http').get('http://localhost:3000', r => process.exit(r.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(1))"]

CMD ["node", "server.js"]
