# Test-only adapters use dependencies already pinned in the service images.
# Production images do not acquire cross-service test dependencies.
FROM mapp-mcp-auth:local AS auth
FROM mapp-config-ui:local AS config

FROM config AS config-tests
COPY --from=auth /usr/local/lib/python3.12/site-packages/authlib /usr/local/lib/python3.12/site-packages/authlib

FROM mapp-mcp:local AS mcp-tests
COPY --from=config /usr/local/lib/python3.12/site-packages/psycopg /usr/local/lib/python3.12/site-packages/psycopg
COPY --from=config /usr/local/lib/python3.12/site-packages/psycopg_binary /usr/local/lib/python3.12/site-packages/psycopg_binary
COPY --from=config /usr/local/lib/python3.12/site-packages/psycopg_binary.libs /usr/local/lib/python3.12/site-packages/psycopg_binary.libs

FROM node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e AS node
FROM mcp-tests AS scripts-tests
USER root
RUN apk add --no-cache bash curl libstdc++ openssl
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=auth /usr/local/lib/python3.12/site-packages/authlib /usr/local/lib/python3.12/site-packages/authlib
COPY --from=config /usr/local/lib/python3.12/site-packages/pglast /usr/local/lib/python3.12/site-packages/pglast
USER mappmcp:mappmcp
