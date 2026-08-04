FROM postgis/postgis:17-3.5@sha256:45f2a608397fa67d236b012c14a9e3ea31e9fe813edbeb5c1c0d1acbf0d48ea9

ARG H3_PG_REF=v4.2.3
ARG H3_PG_COMMIT=a26630b8353d441e6bc8065c0a8dcaa3d89ef87b

RUN set -eux; \
    sed -i 's|http://deb.debian.org/debian-security|https://security.debian.org/debian-security|g' /etc/apt/sources.list; \
    sed -i 's|http://deb.debian.org/debian|https://ftp.debian.org/debian|g' /etc/apt/sources.list; \
    apt-get -o Acquire::Retries=10 update; \
    apt-get -o Acquire::Retries=10 install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      cmake \
      git \
      postgresql-server-dev-17; \
    git clone --branch "${H3_PG_REF}" --depth 1 \
      https://github.com/postgis/h3-pg.git /tmp/h3-pg; \
    test "$(git -C /tmp/h3-pg rev-parse HEAD)" = "${H3_PG_COMMIT}"; \
    cmake -S /tmp/h3-pg -B /tmp/h3-pg/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DPostgreSQL_INDENT=PostgreSQL_INDENT-NOTFOUND; \
    cmake --build /tmp/h3-pg/build --parallel; \
    cmake --install /tmp/h3-pg/build --component h3-pg; \
    rm -rf /tmp/h3-pg /var/lib/apt/lists/*; \
    apt-get purge -y --auto-remove \
      build-essential cmake git postgresql-server-dev-17
