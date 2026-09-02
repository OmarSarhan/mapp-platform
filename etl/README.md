# Bundled geospatial ETL

This is a deliberately small, one-shot sample-data loader for three public
Leeds layers. It is not required by the MAPP runtime and does not mirror the
whole `Public` folder. Federated source databases are expected to manage their own data. The
runtime image contains the code and a baked example manifest; deployment
uses `/config/layers.json`, mounted from `instance/etl/layers.json` on the
host.

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

## Optional England Census 2021 Output Area load

The Census loader is a separate, explicit data-provisioning operation. It loads
the latest full census for England, Census 2021, whose estimates refer to
Census Day, 21 March 2021. Its official sources are:

- [Nomis Census 2021 bulk topic
  summaries](https://www.nomisweb.co.uk/census/2021/bulk), using the reviewed
  per-topic ZIP archives and their embedded metadata
- the ONS [Output Areas (December 2021) Boundaries EW BGC
  (V2)](https://geoportal.statistics.gov.uk/datasets/ons::output-areas-december-2021-boundaries-ew-bgc-v2/about),
  filtered to England and joined by `OA21CD`

The reviewed manifest contains 47 topic summaries with 467 numeric measures.
Every topic is filtered to codes matching `^E\d{8}$` and must contain the same
178,605 England 2021 Output Areas as the geometry source. Wales, Scotland, and
Northern Ireland are outside this dataset. Topic products without published OA
data are not substitutes: TS010 currently has an empty OA member, TS079 has
returned an empty invalid archive, and TS032 through TS036 contain Wales-only
OA rows. Those products and every other topic without the reviewed England OA
contract are excluded.

Validate all pinned sources, or one topic, without changing the database:

```sh
./bin/mapp census-check
./bin/mapp census-check TS001
```

Run the complete load through the demo, which loads it into the `census-db`
source database rather than into the platform's own:

```sh
./bin/mapp demo
```

`census-check` reads the publisher and writes to no database, so it works
whatever state the sources are in. The platform database holds no spatial data
at all, so `./bin/mapp reset-data --confirm` does not remove the Census
snapshot: it lives in the `census-db` volume and survives. Rerun
`./bin/mapp demo` when you want the sources reloaded from their publishers.
The 47 topic archives alone are approximately 152 MiB compressed, before the
boundary download and working space. The 467 double-precision values for
178,605 rows require approximately 636 MiB (667 MB) before PostgreSQL row,
geometry, metadata, TOAST, and index overhead. Narrow topic staging, the
assembled snapshot, the stable table, and a prior snapshot can coexist briefly
during an atomic refresh. PostgreSQL staging and write-ahead log use the
database volume; the ETL container's `/tmp` is only the bounded archive spool.
Treat 6 GiB free as a minimum planning floor for this reviewed dataset, not as
a maximum or capacity guarantee, and leave additional headroom for WAL,
maintenance, and unrelated relations.

The stable published relation is `leeds.census_2021_england_oa`, with one row
and one geometry per `OA21CD`. Measure column names do not depend on mutable
human labels. They are the lower-case topic ID followed by a four-digit,
one-based source-measure ordinal in source-header order, for example
`ts001_0001`. A metadata catalogue maps every generated column back to its
topic, ordinal, original Nomis label, and source definition. Dataset metadata
also retains the canonical URL, OA and metadata member names, archive byte
count and SHA-256, and the embedded title, issue date, and version. Each
available bounded Nomis metadata document is retained once in the dataset
record as decoded UTF-8 text together with the SHA-256 of its exact raw bytes.
Per-variable records keep only compact source identifiers and hashes, not 467
copies of those documents. TS007A has no official metadata member, so its
member, document hash, and text remain explicitly null rather than inferred.
The stable table and all measure columns also receive PostgreSQL comments from
the official labels so generic semantic introspection can explain compact
column names without reading source rows.

The dataset record, run record, and successful-load JSON expose the actual
`geometry_repairs` count. Dataset geometry metadata also retains the
deterministically sorted `OA21CD` repair-candidate list, whose length must equal
that count, plus the full canonical ArcGIS layer metadata covered by the
geometry pin. That layer metadata preserves source-supplied identifiers and
revision information such as `serviceItemId` and `editingInfo`. Consumers must
use the catalogue rather than infer meaning from compact SQL names.

Source checks are intentionally exact. Both commands validate each configured
archive byte count and SHA-256, ZIP integrity and expected members, the
reviewed header and measure count, numeric values, 178,605 unique England OA
codes, and a complete OID-ordered scan of all 178,605 geometry features. The
geometry scan requires unique valid England `OA21CD` values and non-null
geometry, and hashes the canonical ArcGIS metadata followed by every canonical
feature digest. That deterministic SHA-256 is pinned in the manifest and is
checked before spatial repair validation or publication. The full load
additionally requires exact OA-code set equality across all 47 topics and the
geometry, plus non-empty valid published polygons. A changed hash, header, row
count, duplicate, missing or extra code, malformed value, or geometry mismatch
fails closed; it is not silently accepted as a publisher update. The manifest
caps permitted source-polygon validity repairs at 64. The currently pinned
official geometry requires 32 repairs; the loader records the observed count
rather than treating that source characteristic as an unreported
normalization.

The loader completes downloads and validation in run-specific staging before
publication. It then replaces the stable relation and publishes its dataset
variable metadata, and column comments together in one atomic transaction
without changing the stable relation's PostgreSQL OID. Publication uses
`TRUNCATE` followed by the complete stable-table insert, so it holds an
`AccessExclusive` lock for the full replacement and can block map, semantic,
or derived-layer readers until commit. Schedule refreshes with that
availability impact in mind. Before the transaction records success, it runs
`ANALYZE` for the OA identifier and the EPSG:4326 and EPSG:3857 geometry
columns. This refreshes relation row estimates and the spatial statistics used
by federated plans without extending the publication lock to analyze all 467
measure columns. Any source, conversion, validation, planner-statistics, or
publication failure rolls back the transaction, leaves the last successful
relation and metadata available, removes staging, and exits non-zero.

Census standard outputs and the boundary product are reusable under the Open
Government Licence. Preserve the source metadata and display the attribution
required by the [ONS geography licence
guidance](https://www.ons.gov.uk/methodology/geography/licences):

```text
Source: Office for National Statistics licensed under the Open Government Licence v.3.0
Contains OS data © Crown copyright and database right [year]
```

`[year]` is deliberately unresolved. Do not expose a map layer, display, or
redistribute the boundary data while that placeholder remains. Resolve the
correct year from authoritative product evidence, configure the complete ONS
and OS attribution on every consuming layer, and record the evidence before
release. Do not guess or hard-code an inferred year.

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

Both geometry columns have GiST indexes. After each complete reconciliation,
the loader explicitly runs `ANALYZE` on the target table before marking that
layer run successful. Control tables `leeds._etl_runs` and
`leeds._etl_layers` hold counts, errors, source metadata, and last-success
state.

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

The separate Census live test samples the current ONS geometry contract and
fully parses both TS001 and the metadata-less TS007A archive. Use
`./bin/mapp census-check` for the complete pinned geometry scan:

```sh
RUN_LIVE_CENSUS_TESTS=1 PYTHONPATH=etl/src \
  python -m unittest discover -s etl/tests -p 'test_live_census_source.py' -v
```

ArcGIS pagination behavior is based on Esri's official
[`query` operation documentation](https://developers.arcgis.com/rest/services-reference/enterprise/query-map-service-layer/),
including `resultOffset`, `resultRecordCount`, `orderByFields`, and `outSR`.
