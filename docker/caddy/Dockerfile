FROM golang:1.26.7-alpine3.23@sha256:b17af760035fc2f338eed92d448a6c67f2d45438844fc6c60678fa5f99e44b57 AS caddy-builder

ARG CADDY_VERSION=v2.11.4
ARG CADDY_COMMIT=e2eee6a7fce366321294c9c2a79f3146891dcbdf

RUN apk add --no-cache git=2.52.0-r0 \
    && git init /reviewed-source \
    && git -C /reviewed-source fetch --depth 1 \
      https://github.com/caddyserver/caddy.git "refs/tags/${CADDY_VERSION}" \
    && test "$(git -C /reviewed-source rev-parse 'FETCH_HEAD^{commit}')" = "${CADDY_COMMIT}" \
    && git -C /reviewed-source checkout --detach FETCH_HEAD \
    && test "$(git -C /reviewed-source rev-parse HEAD)" = "${CADDY_COMMIT}"

WORKDIR /build

RUN go mod init mapp.invalid/caddy-build \
    && go mod edit -require="github.com/caddyserver/caddy/v2@${CADDY_VERSION}" \
    && go mod edit -replace="github.com/caddyserver/caddy/v2=/reviewed-source" \
    && go get "github.com/caddyserver/caddy/v2/cmd/caddy@${CADDY_VERSION}" \
    && go get \
      golang.org/x/text@v0.39.0 \
      google.golang.org/grpc@v1.82.1 \
    && mkdir -p /out \
    && CGO_ENABLED=0 go build \
      -trimpath \
      -ldflags "-s -w -X github.com/caddyserver/caddy/v2.CustomVersion=${CADDY_VERSION}" \
      -buildvcs=false \
      -o /out/caddy \
      github.com/caddyserver/caddy/v2/cmd/caddy \
    && go version -m /out/caddy \
    && /out/caddy version

FROM caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

RUN apk add --no-cache --upgrade \
      c-ares=1.34.8-r0 \
      curl=8.20.0-r0 \
      libcurl=8.20.0-r0

COPY --from=caddy-builder /out/caddy /usr/bin/caddy
