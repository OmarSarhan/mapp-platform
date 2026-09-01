# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e AS build

ARG XYZ_REPOSITORY=https://github.com/GEOLYTIX/xyz.git
ARG XYZ_REF=v4.23.4
ARG XYZ_COMMIT=a6f03c07dd7aaae2e9ab04087143ee0400e15cb9
ARG XYZ_DB_POOL_CONNECTIONS=20

RUN apk add --no-cache \
      ca-certificates \
      g++ \
      git \
      make \
      python3

WORKDIR /app

RUN set -eux; \
    git clone --branch "${XYZ_REF}" --depth 1 "${XYZ_REPOSITORY}" xyz; \
    cd xyz; \
    test "$(git rev-parse HEAD)" = "${XYZ_COMMIT}"; \
    grep -F "max: ${XYZ_DB_POOL_CONNECTIONS}," mod/utils/dbs.js

WORKDIR /app/xyz

# Upstream declares pnpm 10.10.0 but has no dependency lock. Express is a
# runtime import listed under devDependencies, so the complete set is needed.
RUN corepack enable \
    && corepack prepare pnpm@10.10.0 --activate \
    && pnpm install --no-frozen-lockfile \
    && pnpm run _build \
    && pnpm store prune \
    && rm -rf .git /root/.cache /root/.local/share/pnpm/store

FROM node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e AS runtime

ARG XYZ_REF=v4.23.4
ARG XYZ_COMMIT=a6f03c07dd7aaae2e9ab04087143ee0400e15cb9

RUN apk add --no-cache ca-certificates \
    && rm -rf \
      /opt/yarn-v1.22.22 \
      /usr/local/lib/node_modules/corepack \
      /usr/local/lib/node_modules/npm \
    && rm -f \
      /usr/local/bin/corepack \
      /usr/local/bin/npm \
      /usr/local/bin/npx \
      /usr/local/bin/yarn \
      /usr/local/bin/yarnpkg

LABEL org.opencontainers.image.title="GEOLYTIX XYZ" \
      org.opencontainers.image.source="https://github.com/GEOLYTIX/xyz" \
      org.opencontainers.image.version="${XYZ_REF}" \
      org.opencontainers.image.revision="${XYZ_COMMIT}" \
      org.opencontainers.image.licenses="MIT"

ENV NODE_ENV=production \
    PORT=3000

WORKDIR /app/xyz

COPY --from=build --chown=node:node /app/xyz /app/xyz
COPY --chown=node:node supervisor.mjs /app/xyz/supervisor.mjs

USER node

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=6 \
  CMD ["node", "--input-type=module", "-e", "import net from 'node:net'; const socket=net.connect(3000,'127.0.0.1',()=>socket.end()); socket.setTimeout(2000,()=>{socket.destroy();process.exit(1)}); socket.on('error',()=>process.exit(1));"]

CMD ["node", "supervisor.mjs"]
