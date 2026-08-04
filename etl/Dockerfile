FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

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

RUN useradd --create-home --uid 10001 etl \
    && chown -R etl:etl /app
USER etl

CMD ["python", "-m", "leeds_arcgis_etl"]
