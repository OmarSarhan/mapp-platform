FROM ubuntu/squid:6.6-24.04_edge@sha256:8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c

RUN apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade \
      libssl3t64=3.0.13-0ubuntu3.12 \
      openssl=3.0.13-0ubuntu3.12 \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 3128
