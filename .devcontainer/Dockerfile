FROM node:22.23.1-bookworm-slim AS node

FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

COPY --from=node /usr/local/bin/ /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules/ /usr/local/lib/node_modules/

RUN mkdir -p /home/vscode/.codex \
    && chown -R vscode:vscode /home/vscode/.codex
