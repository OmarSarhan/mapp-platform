FROM python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_RUNTIME_SOCKET=/run/mapp-mcp/mapp-mcp.sock

RUN addgroup -S -g 10003 mappmcp \
    && adduser -S -D -H -h /nonexistent -u 10003 -G mappmcp mappmcp \
    && mkdir -p /app /run/mapp-mcp \
    && chown mappmcp:mappmcp /app /run/mapp-mcp \
    && chmod 0755 /app \
    && chmod 0770 /run/mapp-mcp

COPY requirements.txt /app/
# One step, unlike mcp-auth's two: every dependency here is pure Python, so
# none of them needs the --no-deps treatment Authlib does.
RUN pip install --no-cache-dir --requirement /app/requirements.txt

COPY --chown=mappmcp:mappmcp VERSION app.py authentication.py canonical.py era_guard.py execution_envelope.py introspection_client.py protected_resource.py runtime.py server.py /app/

USER mappmcp:mappmcp
WORKDIR /app

# Over the socket, because that is the only thing this process listens on. The
# metadata document is the right probe: it is unauthenticated, it is served by
# the application rather than by the server, and it fails if the ASGI stack did
# not come up.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import http.client, os, socket; c = http.client.HTTPConnection('localhost'); c.sock = socket.socket(socket.AF_UNIX); c.sock.settimeout(2); c.sock.connect(os.environ['MCP_RUNTIME_SOCKET']); c.request('GET', '/.well-known/oauth-protected-resource/mcp'); r = c.getresponse(); raise SystemExit(0 if r.status == 200 else 1)"]

CMD ["python", "/app/server.py"]
