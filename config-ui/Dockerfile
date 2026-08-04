FROM node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS web
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY index.html ./
COPY src ./src
COPY static ./static
RUN npm run build && cp static/advanced-configuration.html dist/

FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY app.py workspace_schema.py plugin_registry.py svg_icons.py static_files.py infoj_types.py control_plane.py control_api.py config_admin.py derived_layers.py derived_query_guard.py semantic_client.py gemini_client.py semantic_sources.py ./
COPY schema ./schema
COPY --from=web /web/dist ./static

USER nobody
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]

CMD ["python", "app.py"]
