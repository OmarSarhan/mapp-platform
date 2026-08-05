FROM golang:1.26.5-alpine3.23@sha256:622e56dbc11a8cfe87cafa2331e9a201877271cbff918af53d3be315f3da88cc AS gosu-builder

ARG GOSU_REPOSITORY=https://github.com/tianon/gosu.git
ARG GOSU_COMMIT=6456aaa0f3c854d199d0f037f068eb97515b7513

RUN apk add --no-cache git=2.52.0-r0 \
    && git init /src \
    && git -C /src fetch --depth 1 "${GOSU_REPOSITORY}" "${GOSU_COMMIT}" \
    && git -C /src checkout --detach FETCH_HEAD \
    && test "$(git -C /src rev-parse HEAD)" = "${GOSU_COMMIT}"

WORKDIR /src

RUN mkdir -p /out \
    && CGO_ENABLED=0 go build \
      -trimpath \
      -ldflags '-d -w' \
      -buildvcs=false \
      -o /out/gosu \
      github.com/tianon/gosu \
    && /out/gosu --version

FROM postgis/postgis:17-3.5-alpine@sha256:978a2e6671c956d650d1f240dba7c73b8519a5f5af8685165fca616cc4ae3568

COPY --from=gosu-builder /out/gosu /usr/local/bin/gosu

ARG H3_PG_REF=v4.2.3
ARG H3_PG_COMMIT=a26630b8353d441e6bc8065c0a8dcaa3d89ef87b

RUN set -eux; \
    apk add --no-cache --virtual .h3-build-deps \
      build-base=0.5-r4 \
      cmake=4.2.3-r0 \
      git=2.54.0-r0 \
      postgresql17-dev=17.10-r0; \
    git clone --branch "${H3_PG_REF}" --depth 1 \
      https://github.com/postgis/h3-pg.git /tmp/h3-pg; \
    test "$(git -C /tmp/h3-pg rev-parse HEAD)" = "${H3_PG_COMMIT}"; \
    cmake -S /tmp/h3-pg -B /tmp/h3-pg/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DPostgreSQL_INDENT=PostgreSQL_INDENT-NOTFOUND; \
    cmake --build /tmp/h3-pg/build --parallel; \
    cmake --install /tmp/h3-pg/build --component h3-pg; \
    rm -rf /tmp/h3-pg; \
    apk del .h3-build-deps; \
    gosu nobody true
