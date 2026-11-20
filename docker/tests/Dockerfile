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
