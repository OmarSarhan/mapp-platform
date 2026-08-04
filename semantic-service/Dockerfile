FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    STATE_DIR=/state

RUN groupadd --gid 10001 semantic \
    && useradd --uid 10001 --gid semantic --no-create-home --home-dir /nonexistent semantic \
    && install -d -o semantic -g semantic -m 0700 /state \
    && install -d -o semantic -g semantic -m 0755 /app

COPY --chown=semantic:semantic VERSION semantic_store.py server.py /app/

USER semantic:semantic
WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=2).read()"]

CMD ["python", "/app/server.py"]
