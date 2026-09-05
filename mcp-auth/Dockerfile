FROM python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_AUTH_CONTROL_PORT=8080 \
    MCP_AUTH_SOCKET=/run/mapp-auth/mapp-auth.sock

RUN addgroup -S -g 10002 mappauth \
    && adduser -S -D -H -h /nonexistent -u 10002 -G mappauth mappauth \
    && mkdir -p /app /run/mapp-auth \
    && chown mappauth:mappauth /app /run/mapp-auth \
    && chmod 0755 /app \
    && chmod 0770 /run/mapp-auth

COPY requirements.txt requirements-nodeps.txt /app/
# Two steps on purpose. psycopg needs its dependencies; Authlib must not have
# them, because its declared set pulls cryptography, which has no musl wheel
# and none of the RFCs this component uses need it.
RUN pip install --no-cache-dir --requirement /app/requirements.txt \
    && pip install --no-cache-dir --no-deps --requirement /app/requirements-nodeps.txt

COPY --chown=mappauth:mappauth VERSION authlib_adapter.py issuer.py models.py pages.py passwords.py server.py sql_store.py stub_store.py unix_server.py /app/

USER mappauth:mappauth
WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_AUTH_CONTROL_PORT', '8080') + '/healthz', timeout=2).read()"]

CMD ["python", "/app/server.py"]
