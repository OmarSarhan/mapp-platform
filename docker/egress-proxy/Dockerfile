FROM ubuntu/squid:6.6-24.04_edge@sha256:8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c

# The base image ships openssl 3.0.13-0ubuntu3.9; this pulls the current
# security update over it. The version is pinned so the build is reproducible,
# which means it stops building once Ubuntu withdraws that exact version in
# favour of a newer one -- 3.12 went that way. That failure is deliberate and
# loud: bump both pins to the current 24.04 security version rather than
# dropping the pin, and check `apt-cache policy openssl` in the base image for
# what that is.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade \
      libssl3t64=3.0.13-0ubuntu3.15 \
      openssl=3.0.13-0ubuntu3.15 \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 3128
