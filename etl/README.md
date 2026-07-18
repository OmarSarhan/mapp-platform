# Leeds ArcGIS to PostGIS ETL

This is a deliberately small, one-shot sample-data loader for three public
Leeds layers. It is not required by the MAPP runtime and does not mirror the
whole `Public` folder. The supported wrapper enables it only with
`MAPP_DATABASE_MODE=bundled`; an external PostGIS deployment is expected to
manage its own data. The runtime image contains the code and a baked example
manifest; deployment uses `/config/layers.json`, mounted from
`instance/etl/layers.json` on the host.

## Selected data

Counts below were observed on 2026-07-17 and will change at the publisher.

| Target table | ArcGIS layer | Geometry | Observed rows | Representative fields |
| --- | --- | --- | ---: | --- |
| `leeds.bus_stops` | [`Transportation/MapServer/0`](https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Transportation/MapServer/0) — Leeds Bus Stops | Point | 4,233 | text, ArcGIS epoch dates |
| `leeds.definitive_paths` | [`PROW/MapServer/4`](https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/PROW/MapServer/4) — Definitive Paths | line | 2,484 | text, double precision |
| `leeds.smoke_control_orders` | [`Planning/MapServer/8`](https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/8) — Smoke Control Orders | polygon | 200 | OID, integer, long text, double precision, dates |

All three layer resources report source EPSG:27700, GeoJSON query support,
ordered pagination, and a `maxRecordCount` of 1,000. The loader requests
`outSR=4326`; Leeds ArcGIS Server performs the reprojection. These are public
sample sources, not an operational-data freshness guarantee.

## Polygon-source selection

The original sample used
[`Planning/MapServer/1`](https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/1)
(`Planning Apps Valid Last Month`). On 2026-07-17 it still returned metadata,
but every tested count and feature query returned ArcGIS error 400. Planning
layers 11 and 12 were rejected as fallbacks: they have different schemas and
meanings and contained 65,642 and 363,050 records respectively.

The reviewed replacement is
[`Planning/MapServer/8`](https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/8)
(`Smoke Control Orders`). It is a bounded Leeds polygon dataset with 200
queryable records and retains the sample's representative integer, long-text,
date, and double-precision mappings. It uses a new
`leeds.smoke_control_orders` table so source identities cannot mix with the
retired planning dataset.

The loader does not destructively remove tables that disappear from the
manifest. An upgraded database can therefore retain the former
`leeds.planning_applications_recent` snapshot, but it is no longer refreshed or
referenced by the versioned workspace seed. After checking that no live
workspace or downstream consumer still uses it, a database owner may archive
or drop it separately.

## Runtime contract

- Docker build context: `./etl`
- Dockerfile: `./etl/Dockerfile`
- Command: `python -m leeds_arcgis_etl`
- Required container environment: `DATABASE_URL`, populated by Compose from
  the operator-facing `.env` key `ETL_DATABASE_URL`
- Optional environment: `ETL_CONFIG` (default `/config/layers.json`),
  `ETL_LAYER` (comma-separated keys), `LOG_LEVEL`
- Optional CLI: `--layer KEY` (repeatable), `--check-source`, `--config PATH`

For the planned network, a typical DSN is:

```text
postgresql://mapp_etl:<password>@db:5432/mapp
```

The baked `etl/config/layers.json` is a versioned example and source-schema
contract. The deployment manifest is versioned separately at
`instance/etl/layers.json` and mounted read-only at `/config`. Change that
instance manifest through normal review; rebuilding the ETL image does not
overwrite it.

## Database ownership and grants

Install PostGIS as an administrator and make the ETL role own its schema. The
loader intentionally does not try to install extensions:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS leeds AUTHORIZATION mapp_etl;
ALTER SCHEMA leeds OWNER TO mapp_etl;
GRANT CONNECT ON DATABASE mapp TO mapp_etl;
GRANT USAGE, CREATE ON SCHEMA leeds TO mapp_etl;
```

PostGIS functions normally retain the default `EXECUTE` grant to `PUBLIC`. On a
hardened database that revoked it, grant `mapp_etl` `USAGE` on the extension
schema and `EXECUTE` for `PostGIS_Version`, `ST_GeomFromGeoJSON`, `ST_SetSRID`,
`ST_Force2D`, `ST_Multi`, and `ST_Transform`.

If XYZ connects with a separate read-only role, grant it access after the ETL
role exists, and set default privileges for tables the ETL will create:

```sql
GRANT USAGE ON SCHEMA leeds TO mapp_xyz;
GRANT SELECT ON ALL TABLES IN SCHEMA leeds TO mapp_xyz;
ALTER DEFAULT PRIVILEGES FOR ROLE mapp_etl IN SCHEMA leeds
  GRANT SELECT ON TABLES TO mapp_xyz;
```

## Data model and rerun behavior

Each data table has selected, typed business columns plus:

- `object_id bigint` — source OID and primary key
- `source_attributes jsonb` — the exact selected ArcGIS properties
- `geom` — canonical Point/MultiLineString/MultiPolygon in EPSG:4326
- `geom_3857 geometry(Geometry,3857)` — stored generated transform for XYZ/MVT
- `source_hash` — SHA-256 of canonical properties and geometry
- first-seen, last-changed, last-seen, and run identifiers

Both geometry columns have GiST indexes. Control tables `leeds._etl_runs` and
`leeds._etl_layers` hold counts, errors, source metadata, and last-success state.

The sources do not consistently expose a trustworthy last-edit watermark. A run
therefore does an ordered `resultOffset`/`resultRecordCount` scan and hash-based
upsert. Unchanged rows keep `last_changed_at`. Source deletions are reconciled
only after the start count, unique fetched OID count, and end count all agree.
Each layer also defines a reviewed `minimum_source_count`; an implausibly small
but internally consistent source response fails before any page is loaded.
Changing that floor is the explicit operational override for a legitimate major
publisher-side reduction. Network, schema, conversion, duplicate-ID,
below-minimum, or count-drift failures retain existing rows from deletion,
record a failed run, and return a non-zero exit code. Because pages commit as
they are processed, a mid-run failure may already have inserted or updated
source rows; it guarantees that unseen prior rows are not deletion-reconciled,
not that the entire prior snapshot is byte-for-byte unchanged. Page upserts
make a retry safe and idempotent. A session-level PostgreSQL advisory lock
serializes each target layer; a second overlapping invocation exits before it
writes a run record or table row.

Known ArcGIS service failures are reported as one concise error rather than a
Python traceback. They still return non-zero so scheduling and monitoring do
not mistake a failed refresh for success. Unexpected programming or database
errors retain their traceback for diagnosis.

## Verification

Source contract and first-page GeoJSON check (no database required):

```sh
PYTHONPATH=etl/src python -m leeds_arcgis_etl \
  --config etl/config/layers.json --check-source
```

Unit tests:

```sh
PYTHONPATH=etl/src python -m unittest discover -s etl/tests -v
```

The opt-in live test queries metadata, count, and a two-record GeoJSON page for
every configured layer:

```sh
RUN_LIVE_ARCGIS_TESTS=1 PYTHONPATH=etl/src \
  python -m unittest discover -s etl/tests -p 'test_live_source.py' -v
```

ArcGIS pagination behavior is based on Esri's official
[`query` operation documentation](https://developers.arcgis.com/rest/services-reference/enterprise/query-map-service-layer/),
including `resultOffset`, `resultRecordCount`, `orderByFields`, and `outSR`.
