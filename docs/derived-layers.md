# Managed derived layers

Managed derived layers expose one read-only PostgreSQL `SELECT` as an
XYZ-compatible relation. The service always creates the result in
`derived_layers`; callers cannot choose another output schema.

| Mode | Behavior | Suitable use |
| --- | --- | --- |
| `view` | Evaluated whenever XYZ reads it. | Indexed sources, modest joins, and results that must immediately follow source changes. |
| `materialized` | Stored until explicitly refreshed. | Expensive spatial joins, H3 generation, and large sources. |

A materialized view receives a unique index on its declared feature ID.
Refresh, replacement, and drop are confirmed, scoped, and audited actions.

Replacement can update a definition or convert between ordinary and
materialized kinds. The service creates and validates a temporary relation
first, then swaps it inside one transaction; failure leaves the original in
place. The dashboard exposes edit, refresh, conversion, and deletion through
each existing layer's **Actions** dropdown. Replacement and drop are refused
for PostgreSQL dependent objects.
Drop is also refused while the live dashboard workspace references
`derived_layers.<name>`. Structured errors report `dropped: false`,
`dependents`, `dependentColumns`, `removedColumns`, `workspaceReferences`, and
`requiresSecondOrderChanges`. Successful replacements report added, removed,
and type-changed output columns plus dashboard field references. External clients that merely
issue reads do not create catalog dependencies and cannot be detected.

Creating the relation does not add it to the workspace. Reload the dashboard
catalog or run `config-cli catalog list`, then create and review a normal
revision-bound workspace proposal referring to `derived_layers.<name>`.
Catalog discovery reads geometry type and SRID directly from PostgreSQL
relation attributes so ordinary tables, views, and materialized views are
reported consistently.
When an edit affects configured workspace fields, CLI clients should show the
same impact and ask whether the necessary second-order workspace operations
should be included in that same revision-bound proposal. Those operations
still require focused-diff review and explicit approval.

API results and errors provide `userMessage` and, where action is needed,
`suggestedAction`. These are the user-facing dashboard and CLI messages.
Machine-oriented paths, database object descriptions, reason codes, and
`technicalDetail` remain available for logs and automation but should only be
shown in an optional technical-details view.

## Database boundary

The configuration service uses `DERIVED_DATABASE_URL`, identifying a role
that owns only `derived_layers` and can read approved source schemas. XYZ
continues to use read-only `DBS_MAPP`; it can select managed outputs but cannot
create, refresh, or drop them. Ordinary views use `security_invoker=true` and
`security_barrier=true`.

New bundled volumes receive the roles, schema, H3 extensions, and grants
automatically. Upgrade an existing bundled volume explicitly after rebuilding
the database image:

```sh
./bin/mapp upgrade-derived
```

External-database operators must provision equivalent roles, grants, PostGIS,
and optional H3 extensions themselves. `DERIVED_READER_ROLE` names the
read-only XYZ database role; the service grants that role `SELECT` on each
published result, but not on the private `_definitions` registry. Leaving
`DERIVED_DATABASE_URL` empty
disables mutation and reports `configured: false`.

## Definition checks

```json
{
  "name": "paths_h3_r9",
  "kind": "view",
  "sources": ["leeds.definitive_paths"],
  "idColumn": "h3_id",
  "geometryColumn": "geom_3857",
  "description": "Resolution-9 H3 cells intersected by a definitive path.",
  "query": "SELECT ..."
}
```

The server requires:

- one `SELECT` or `WITH ... SELECT`, without comments or terminators;
- schema-qualified relations declared in `sources`;
- PostgreSQL's recorded relation dependencies to exactly match `sources`;
- a typed PostGIS geometry with a positive SRID;
- a non-null, unique feature ID and at least one non-null geometry;
- completion within bounded statement and lock timeouts.

DDL, DML, session changes, notifications, copying, and dependencies on another
managed derived layer are rejected. This remains trusted administrative SQL:
grant `derive` only to trusted operators and review query cost before creation.

## H3 support and example

The bundled image builds
[`h3-pg` v4.2.3](https://github.com/postgis/h3-pg/tree/a26630b8353d441e6bc8065c0a8dcaa3d89ef87b)
from its pinned full commit and installs `h3` and `h3_postgis`. H3 PostGIS
functions expect EPSG:4326 longitude/latitude and do not reproject input.

This query generates resolution-9 candidates and lets exact PostGIS
intersection determine which cells a Definitive Path touches:

```sql
WITH path_extent AS (
  SELECT ST_Envelope(ST_Collect(ST_Transform(geom_3857, 4326))) AS geom
  FROM leeds.definitive_paths
),
candidate_cells AS (
  SELECT
    cell AS h3,
    h3_cell_to_boundary_geometry(cell) AS geom_4326
  FROM path_extent
  CROSS JOIN LATERAL h3_polygon_to_cells(
    ST_Buffer(geom::geography, 500)::geometry,
    9
  ) AS cell
),
projected_cells AS (
  SELECT
    h3,
    ST_Transform(geom_4326, 3857)::geometry(Polygon, 3857) AS geom_3857
  FROM candidate_cells
)
SELECT
  h3::text AS h3_id,
  geom_3857
FROM projected_cells AS cell
WHERE EXISTS (
  SELECT 1
  FROM leeds.definitive_paths AS path
  WHERE path.geom_3857 && cell.geom_3857
    AND ST_Intersects(path.geom_3857, cell.geom_3857)
)
```

Declare `leeds.definitive_paths` as the source, `h3_id` as the ID, and
`geom_3857` as geometry. The 500-metre extent margin prevents edge candidates
being omitted; it does not buffer the final intersection. Prefer a
materialized view when repeated H3 generation is too expensive.

## API shared by dashboard and CLI

| Route | Scope | Purpose |
| --- | --- | --- |
| `GET /api/derived-layers/capabilities` | `inspect` | Modes and PostGIS/H3 versions |
| `GET /api/derived-layers` | `inspect` | Definitions without SQL |
| `GET /api/derived-layers/{name}` | `inspect` | One definition including SQL |
| `POST /api/derived-layers` | `derive` | Create a view or materialized view |
| `POST /api/derived-layers/{name}/refresh` | `derive` | Confirmed materialized refresh |
| `POST /api/derived-layers/{name}/replace` | `derive` | Confirmed atomic replacement or kind conversion |
| `POST /api/derived-layers/{name}/drop` | `derive` | Confirmed dependency-safe removal |
| `POST /api/derived-layers/{name}/drop` | `derive` | Confirmed removal |

Both clients use these routes. They never receive the database credential and
do not duplicate server validation.
