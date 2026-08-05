FROM python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ETL_CONFIG=/config/layers.json \
    ETL_CENSUS_CONFIG=/config/census.json

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config

RUN addgroup -S -g 10001 etl \
    && adduser -D -h /home/etl -u 10001 -G etl etl \
    && chown -R etl:etl /app
USER etl

CMD ["python", "-m", "leeds_arcgis_etl"]
