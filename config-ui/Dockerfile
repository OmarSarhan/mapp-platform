FROM node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e AS web
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY index.html ./
COPY src ./src
COPY static ./static
RUN npm run build && cp static/advanced-configuration.html dist/

FROM python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY app.py workspace_schema.py plugin_registry.py svg_icons.py static_files.py infoj_types.py control_plane.py control_api.py config_admin.py derived_layers.py derived_query_guard.py semantic_client.py gemini_client.py semantic_sources.py runtime_database.py relation_identity.py federation_schema.py federation_capability.py federation_store.py ./
COPY schema ./schema
COPY --from=web /web/dist ./static

USER nobody
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]

CMD ["python", "app.py"]
