FROM node:22.23.1-bookworm-slim AS web
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY index.html ./
COPY src ./src
COPY static ./static
RUN npm run build

FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY app.py workspace_schema.py svg_icons.py static_files.py infoj_types.py control_plane.py control_api.py config_admin.py ./
COPY schema ./schema
COPY --from=web /web/dist ./static

USER nobody
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]

CMD ["python", "app.py"]
