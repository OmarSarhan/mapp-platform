FROM python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    STATE_DIR=/state

RUN addgroup -S -g 10001 semantic \
    && adduser -S -D -H -h /nonexistent -u 10001 -G semantic semantic \
    && mkdir -p /state /app \
    && chown semantic:semantic /state /app \
    && chmod 0700 /state \
    && chmod 0755 /app

COPY --chown=semantic:semantic VERSION semantic_store.py server.py /app/

USER semantic:semantic
WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=2).read()"]

CMD ["python", "/app/server.py"]
