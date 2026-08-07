# Managed derived layers

Managed derived layers expose one read-only PostgreSQL `SELECT` as an
XYZ-compatible relation. The service always creates the result in
`derived_layers`; callers cannot choose another output schema.

| Mode | Behavior | Suitable use |
| --- | --- | --- |
| `view` | Evaluated whenever XYZ reads it. | Results that must immediately follow source changes, including compute-safe results too large to store as a managed materialized view. |
| `materialized` | Stored until explicitly refreshed. | Expensive but bounded spatial joins or H3 aggregation that pass both compute and materialization-size probes. |

A materialized view receives a unique index on its declared feature ID and a
GiST index set for its declared geometry: the native SRID, canonical EPSG:4326
and EPSG:3857 expressions when they differ, and a safe EPSG:4326 geography
expression. Projected geometry is transformed to EPSG:4326 before the geography
cast. Refresh, replacement, and drop are confirmed, scoped, and audited actions.
Before any view or materialized view is created or replaced—and before a
materialized view is refreshed—PostgreSQL plans the exact map-scoped query and
the service recursively checks its computation budget. A computation failure is
a hard block for both kinds; converting it to an ordinary view is not a bypass.

Materialized operations have a second guard. The service conservatively
estimates stored size from the planned row count and row width, per-row overhead,
and a storage allowance. An estimate above 1 GiB is a hard block: the response
reports the probe and prompts the operator to use an ordinary view only when the
query has already passed the universal computation guard.

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

Every successful managed create also stores a generated semantic-profile event
as a matter of course. The relation definition, stable semantic asset ID,
generation, and event are committed together in PostgreSQL; delivery to the
private semantic service follows through a transactional outbox. Therefore the
existing `derive` permission is enough to create the generated profile. It does
not grant permission to edit curated meaning or apply a semantic proposal.

Create, replace, and refresh results include `semanticProfile` with its stable
`assetId`, current `generation`, semantic catalog `revision` when known, and
one of these public states:

- `registering`: the database commit and durable event succeeded, but delivery
  of that generation is not yet confirmed;
- `ready`: the current generation is present in the semantic catalog; or
- `repair_required`: delivery returned a permanent conflict or exhausted its
  retries, so the retained event blocks later generations until a semantic
  administrator resolves the cause and explicitly retries it.

The configuration service retries transient delivery in the background and
after restart. Delivery is strictly ordered per asset and managed derived
name. PostgreSQL atomically assigns each eligible event an expiring claim;
unexpired claims exclude other workers, and only the matching claimant can
record delivery, retry, or repair. The administrator route named `repair`
requeues the same retained event and payload; it does not correct a
deterministic conflict or corrupt payload. A new workspace publication that
introduces `derived_layers.<name>` is blocked until its current profile is
`ready`.
Unchanged existing references may remain with a warning, and removal is still
allowed. This readiness gate does not combine derived creation with workspace
approval: the operator must still inspect and approve the separate
revision-bound workspace proposal.

Replacement and refresh retain the asset ID and increment its source
generation. Dropping the relation emits an archive event in the same
transaction and leaves a semantic tombstone and history rather than deleting
the profile. See [Semantic metadata control plane](semantic-layer.md).

Derived-profile reads obtain their top-level catalog revision from the live
semantic service and fail unavailable instead of inferring it locally.
Administrators also receive a bounded name-level delivery diagnostic for the
blocking event; ordinary inspectors receive only readiness.

When an edit affects configured workspace fields, CLI clients should show the
same impact and ask whether the necessary second-order workspace operations
should be included in that same revision-bound proposal. Those operations
still require focused-diff review and explicit approval.

After a successful replacement in the workspace dashboard, its catalog is
refreshed immediately and raw workspace layers that directly reference the
derived relation are reconciled in memory:

- added non-geometry, non-ID columns become generated `infoj` entries;
- direct-column `infoj` entries for removed columns are removed;
- calculated `fieldfx` entries are preserved because their result field is an
  alias rather than necessarily being the removed source column;
- removed columns are pruned from filter include/exclude lists and invalid
  hover-field selections are cleared.
- removed or type-changed fields used by direct, named, multi-field, or
  category-level symbology are retained for deliberate correction and marked
  **Derived-layer symbology needs inspection** in the layer editor.

The resulting workspace is deliberately marked unsaved. The operator must
review the updated fields and use **Save & reload XYZ** separately. Replacing a
derived relation is not approval to publish a workspace change. Named-locale
effective views remain read-only and inherit reconciled default-layer fields;
focused raw named overrides still require API/CLI proposals.

The replacement API includes those symbology uses in `fieldReferences`, with
precise workspace paths, consumer labels, and `requiresSecondOrderChanges`.
CLI clients must offer an explicit correction path—select a replacement field,
change symbology mode, refresh/reinspect the derived relation, or abandon the
workspace proposal. They must not infer a replacement merely because a new
column has a similar name or type.

Ordinary views follow source data immediately. Materialized views do not:
their list entry displays the last refresh time and offers **Refresh data**.
Refreshing data does not reconcile schema references; replacing a definition
does. After either action, inspect the returned timestamps/column changes and
reload the catalog before proposing dependent workspace edits.

Guard errors provide `userMessage` and `suggestedAction` as their primary
operator guidance. Each structured `reasons` entry has its own `code`,
`message`, and reason-specific `suggestedAction`, so clients should render the
reasons as a list rather than flattening them into one generic H3 or cost hint.
The envelope also identifies the `operation`, and query-guard errors identify
their `category`. When the database state is known to be preserved,
`stateUnchanged: true` and the
operation-specific `safeState` explain exactly what remains: for example, no
layer was created, the original definition remains active, or the existing
materialized data was not refreshed. Machine-oriented paths, database object
descriptions, reason codes, probes, and `technicalDetail` remain available for
logs and automation, but `technicalDetail` must never replace the primary user
message or appear without an explicit technical-details view.

Other lifecycle failures use the same user-message/action convention. A
`derived_layer.source_mismatch` response reports `declaredSources`,
`resolvedSources`, `missingSources`, and `extraSources` so the operator can
correct the declaration exactly. Missing semantic profiles, invalid spatial
scope, malformed request fields, maintenance, duplicate names, missing layers,
and dependencies have distinct stable codes and corrective actions. A safe
`derived_layer.database_error` does not expose the raw PostgreSQL exception as
its primary message. Its optional `technicalDetail` object contains only a
bounded SQLSTATE and PostgreSQL primary message—not the query, context, detail,
or hint. A preflight database failure or a failure followed by a proven
transaction rollback includes the strongest unchanged-state claim the server
can make. A commit, rollback-finalization, or result-reporting uncertainty is
explicitly indeterminate and requires authoritative inspection before retry.

## Database boundary

The configuration service uses `DERIVED_DATABASE_URL`, identifying a role
that owns only `derived_layers` and can read approved source schemas. XYZ
continues to use read-only `DBS_MAPP`; it can select managed outputs but cannot
create, refresh, or drop them. Ordinary views use `security_invoker=true` and
`security_barrier=true`.

New bundled volumes receive the roles, schema, H3 extensions, grants, and the
restricted-path setting required by the H3 PostGIS polygon SQL wrappers
automatically. Bundled ETL completion also prepares every managed geometry and
geography source column with native, EPSG:4326, EPSG:3857, and safe cross-cast
GiST indexes, then refreshes planner statistics. Upgrade an existing bundled
volume explicitly after rebuilding the database image; the upgrade is
idempotent and repairs both source and existing materialized spatial indexes:

```sh
./bin/mapp upgrade-derived
./bin/mapp verify
```

The verifier does not create indexes. It fails readiness if a managed spatial
column has a generic or unknown SRID, lacks a valid native GiST index, or lacks
one of the prepared canonical/cross-cast indexes. This keeps readiness checks
non-mutating while the ETL and upgrade paths perform the required DDL.

External-database operators must provision equivalent roles, grants, PostGIS,
and optional H3 extensions themselves. `DERIVED_READER_ROLE` names the
read-only XYZ database role; the service grants that role `SELECT` on each
published result, but not on the private `_definitions` registry. Leaving
`DERIVED_DATABASE_URL` empty
disables mutation and reports `configured: false`.

For a complete administrator-facing checklist and parameterized grant
examples, use the
[external PostgreSQL administrator handoff](external-postgresql.md). In
particular, ordinary views use invoker permissions: both the derived owner and
the runtime reader need direct `SELECT` access to every approved source
relation used by an ordinary derived view.

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

- managed relation names to start with `a-z` and then use only lowercase
  `a-z`, `0-9`, or `_`, with at most 63 characters
  (`^[a-z][a-z0-9_]{0,62}$`); the selected ID and geometry column names follow
  the same rule;
- one `SELECT` or `WITH ... SELECT`, without comments or terminators;
- schema-qualified relations declared in `sources`;
- execution by the derived owner with an effective `search_path` fixed to
  `pg_catalog, public`, independent of every schema-qualified source lookup;
- schema-qualified casts to allowlisted PostGIS/H3 types only when pre-analysis
  resolves the exact type as a member of the expected extension and the
  qualifier is the controlled `public` namespace and that extension's
  authoritative namespace;
- a ready PostgreSQL semantic profile for every declared source relation;
- PostgreSQL's recorded relation dependencies to exactly match `sources`;
- an explicit PostGIS geometry typmod with an allowed geometry subtype and a
  positive SRID on the selected output column; a generic `geometry` column with
  only a runtime SRID does not satisfy this output check;
- a non-null, unique feature ID and at least one non-null geometry;
- a bounded query shape and H3 expansion, followed by a recursively bounded
  PostgreSQL plan for every ordinary or materialized result;
- completion within bounded statement and lock timeouts.

An agent must search and show the semantic catalog before choosing relation,
field, geometry, unit, join, or aggregation meaning. Stored semantic meaning
overrules a CLI-side inference. If no suitable source profile exists, a caller
with `semantic:inspect + semantic:source` may list allowlisted relation
identities, explicitly synchronize the selected relation, and then inspect its
generated profile before writing SQL. This fallback does not expose rows and
does not permit guessing an undeclared source. PostgreSQL/PostGIS/H3 functions
inside the query are executable vocabulary rather than relation sources, so
they do not need semantic profiles.

Use names such as `road_lengths_h3_r9`; do not use spaces, hyphens, dots,
uppercase or quoted mixed-case names. The output schema is always
`derived_layers`, so `name` itself must not be schema-qualified. These
ASCII-only names stay within PostgreSQL's 63-byte identifier limit, and the
service still quotes every identifier when constructing database statements.

### Fixed workspace map extent

Every create and replace retains only output features that intersect a fixed
extent around a workspace map centre. Callers may send the following selector
to choose a named locale; omitting it selects the default effective locale:

```json
{
  "spatialScope": {
    "type": "workspace-map-extent",
    "locale": "locale"
  }
}
```

The server reads that locale's effective `view.lng`, `view.lat`, and `view.z`,
calculates a 1920×1080 Web Mercator viewport at `max(0, view.z - 1)`, and
records one EPSG:4326 envelope, or two when the viewport crosses the
antimeridian. Workspace latitudes up to ±90 are accepted and clamped to the
Web Mercator limit for the calculation. Missing, non-finite, or otherwise
invalid views fail clearly.

Use `GET /api/derived-layers/map-extent?locale=KEY` to preview the resolved
`spatialScope`; this read does not require derived-database configuration.
The preview is advisory because the workspace can change afterwards. The
`spatialScope` returned by create or replace is the authoritative extent that
was stored. Callers provide only the scope type and optional locale: supplied
bounds or other resolved fields are rejected and never trusted.

The managed relation wraps the submitted `SELECT` with an output-row
`ST_Intersects` predicate against its declared geometry column. Geometry is
not clipped, so a feature crossing the boundary remains complete. The original
reviewed SQL remains in the registry and the show response while the resolved
scope is stored separately and included in the generated semantic profile.
Refresh retains the saved scope. A replacement always resolves a scope too;
when the selector is omitted it uses the current default effective locale, and
the dashboard preserves the previously selected locale when editing.

This is an output-row guard, not a security boundary or an input-query
optimizer. It does not prevent the submitted query from reading its declared
sources. The complete submitted query runs inside the guard, so layer-level
aggregates and windows use their full declared input before final geometry rows
are area-filtered; neither the size probe nor the guard replaces that input
with a sample. If a metric is intentionally meant to aggregate only map-area
source rows, put the previewed envelope inside the source-side SQL before the
aggregation as well.

For example, a resolution-9 cell's point count may use only points intersecting
that candidate cell, while an information field representing the cell's share
of **all** points must divide by a count over the complete declared point
source. Do not reuse a map-filtered or sampled denominator unless the requested
meaning is explicitly “share of points in this saved map area.”

### Query-computation probe

Every create, replace, and materialized refresh first runs non-writing
`EXPLAIN (FORMAT JSON)` over the exact executable query, including the saved
spatial guard. The service recursively inspects the plan rather than checking
only its final row count. It blocks plans estimated above any of these limits:

| Measure | Limit |
| --- | ---: |
| PostgreSQL total cost | 50,000,000 |
| Final rows | 10,000,000 |
| Rows at any plan node | 100,000,000 |
| Estimated data at any plan node | 16 GiB |
| Join rows relative to the largest child | 1,000× |
| Plan nodes | 150 |
| Plan depth | 32 |
| Sum of planned workers | 8 |

Recursive plans are always rejected. Before planning, the pinned PostgreSQL
parser validates exactly one read-only `SELECT` as an AST. It rejects recursive
or modifying CTEs, `SELECT INTO`, row locks, reserved `_mapp_*` bindings,
unqualified base relations, explicit system/managed-schema reads,
implicit/NATURAL/Cartesian joins, OR-connected join predicates, and predicates
that do not reference both input sides. The AST supplies exact join,
CTE, set-operation, and expanded `CUBE`/`ROLLUP` grouping counts instead of
inferring them from SQL text.

Only syntactically proved bounded set functions are admitted: literal-bounded
`generate_series`, server-scope H3 polygon generation, bounded H3 grid distance,
and immediate H3 children. JSON, array, regular-expression, XML/JSON table, and
PostGIS dump/grid/subdivide row expansion is rejected. Growing transition-state
aggregates and per-row scalar or geometry constructors such as `array_agg`,
`string_agg`, geometry union/collection, `repeat`, `array_fill`,
`ST_GeneratePoints`, and configured buffer/segment construction are rejected;
numeric `count`, `sum`, `avg`, `min`, and `max` remain available.

Before `EXPLAIN`, the service creates the exact executable query as a transient
ordinary view inside a savepoint. PostgreSQL therefore resolves relation,
function, operator, cast, and type OIDs without reading source rows. Declared
relation dependencies must match exactly, and routines must be genuine
`pg_catalog` objects or members of the approved `postgis`, `h3`, or
`h3_postgis` extensions. Volatile, set-returning without an AST proof,
`SECURITY DEFINER`, unapproved-language, custom wrapper/operator/cast/type,
dynamic-query, file, large-object, and server-control dependencies are
rejected. Routine configuration is also rejected except for the H3 PostGIS
polygon SQL wrappers: those extension-owned routines must pin `search_path`
to `pg_catalog` first plus all and only the distinct authoritative namespaces
of the installed allowlisted extensions. Comparison follows PostgreSQL
identifier quoting and ignores harmless whitespace and extension-schema order;
duplicates, `$user`, temporary schemas, missing schemas, unrelated schemas, and
additional routine settings remain rejected. The catalog must prove both object
and implementation provenance; a same-named custom routine or any wider setting
is rejected. The savepoint is rolled back before the five-second `EXPLAIN`; the
same catalog checks run again on the created relation before materialized
population and before every refresh. Every derived database connection first
pins its session `search_path` to `pg_catalog, public`; catalog OID and
extension-membership checks remain the authority.

Schema-qualified PostGIS/H3 cast types are a narrow exception to fixed-search-
path type lookup. Before transient-view analysis, the server resolves the exact
schema and type through the PostgreSQL catalogs, verifies exact membership in
the allowlisted extension, and requires the qualifier to be both the controlled
`public` namespace and that extension's authoritative namespace. A matching
type name in another schema is rejected.
For PostGIS `geometry(...)` casts, only allowlisted geometry typmods with a
positive literal SRID are accepted. This cast admission does not relax output
validation: the selected geometry attribute must still retain that explicit
typmod and positive SRID.

H3 capability readiness is a staged, fail-closed check. It requires PostGIS
3.5.x, matching H3 and H3 PostGIS 4.2.x extension versions, the exact
extension-owned `h3_polygon_to_cells(geometry, integer)` overload, and the same
routine policy used by submitted queries. PostgreSQL then plans and executes a
tiny synthetic polygon call, and the server validates its aggregate result.
The probe reads no source relation or user row.

On success, `h3Readiness` contains only
`method: "postgresql-catalog-and-execution"` and `ready: true`. On failure it
also contains `code: "derived_layer.h3_not_ready"`, one closed `stage`, and a
bounded `reasons` list whose entries have `code`, `message`, and
`suggestedAction`. The stages are `extension-discovery`, `version-validation`,
`catalog-resolution`, `routine-policy`, `nested-dependency-resolution`,
`execution-probe`, and `result-validation`. Diagnostics never include raw SQL,
PostgreSQL error text, connection context, secrets, or database-supplied object
names. `h3Available` is always equal to `h3Readiness.ready`; ordinary non-H3
derived queries remain available when H3 is not ready.

Successful mutations include the unchanged `queryPlanProbe` plus the additive
`queryPlanningProbe`. Capabilities advertise the ordered AST/catalog/EXPLAIN
`stages`, `shapeLimits`, plan `limits`, H3 bounds, and `errorCategories` in
`queryGuard`. The separate versioned `queryPlanning` capability advertises a
100,000,000-row nested-loop pair limit and the
`nested_loop_pair_work` reason code.

The planning probe combines literal `generate_series` bounds and the existing
scoped, composed H3 estimate with PostgreSQL's plan. A literal series is a
per-invocation bound, so a `ProjectSet` multiplies it by the corrected input
rows; the H3 estimate is already the total scoped pipeline bound and is not
multiplied again. The probe carries those conservative bounds through filters,
windows, grouped aggregates, grouping, uniqueness, other bound-preserving plan
nodes, and CTE scans. Only a proven global aggregate or an exact false one-time
filter stops propagation. For each `Nested Loop`, it then multiplies the two
input row estimates.
This is not tied to H3, a spatial predicate, a source table, or a particular SQL
template. A parameterized index scan whose inner `Plan Rows` is small remains
admissible, and hash joins are not charged nested-loop pair work.

Failures distinguish a malformed query, a forbidden query, and an allowed
query whose work is too large:

| HTTP/code | Meaning | Correct response |
| --- | --- | --- |
| `400` / `derived_layer.query_invalid` | `category: "invalid"`; the input is not exactly one parseable `SELECT` statement. | Correct the syntax or statement form; changing layer kind or H3 resolution does not fix it. |
| `422` / `derived_layer.query_not_allowed` | `category: "policy"`; the query reaches a prohibited statement, schema, relation, routine, operator, cast, type, or other catalog dependency. | Follow each reason's `suggestedAction`, schema-qualify approved objects, and use approved PostgreSQL/PostGIS/H3 functionality directly. |
| `409` / `derived_layer.query_too_expensive` | `category: "compute"`; SQL shape, generated/H3 rows, join fan-out, recursion, or the PostgreSQL plan exceeds a resource limit. | Reduce expansion or intermediate work, filter or pre-aggregate earlier, or use a coarser H3 resolution where that still meets the requested semantics. |

All three responses use `blocked: true`, structured `reasons`, and an
operation-specific unchanged-state message. Each is forbidden for both
ordinary and materialized views and none has `recommendedKind`: an ordinary
view is not a syntax, policy, or computation bypass. A planner rejection also
includes the closed legacy `probe`. A `nested_loop_pair_work` rejection also
includes `queryPlanningProbe` beside `probe`, with only its method/version,
proven generator maximum, nested-loop count, estimated pair maximum, and
allowed pair maximum. Keep complete-input totals semantically separate from
the selective row-matching path so PostgreSQL can use a parameterized or
indexed inner plan; do not push a map predicate into a total that is meant to
cover the complete source. The server repeats the checks at the database
mutation boundary so an accepted background request cannot bypass them.
Planner estimates are a conservative admission guard, not a promise of actual
runtime resource use; database timeouts remain a second backstop.

### Materialization-size probe

Ordinary views store no result rows and are the permitted fallback for an output
that is too large to materialize only after its computation probe passes. For
`materialized` create, replace/conversion, and refresh, the same non-writing plan
also estimates materialized storage as
`planned rows × (planned row width + 32 bytes) × 1.2`. The capabilities response
advertises the 1 GiB maximum and the successful mutation result includes
`materializationProbe` so the estimate is reviewable.

When the estimate exceeds the maximum, no materialized DDL or refresh is
started. The API returns HTTP `409` with
`code: "derived_layer.materialization_too_large"`, `blocked: true`, the closed
probe object, `probeStage: "estimate"`, and `recommendedKind: "view"`. The
operation-specific `suggestedAction` says to create or convert to an ordinary
view as appropriate, or to reduce the output; a failed refresh must not tell
the operator to create a duplicate layer. The dashboard asks whether to switch
or convert, but does not submit that different kind until the operator reviews
and submits it. The server repeats the probe at the database mutation boundary,
including after an accepted background request, so another client cannot
bypass the guard.

For an admitted materialization, the service first creates the materialized
view `WITH NO DATA`, rechecks its resolved dependencies, then populates it,
builds the unique ID index, validates its rows, and measures
`pg_total_relation_size`. If the table, TOAST data, and indexes together exceed
1 GiB, the same materialization-too-large error is returned with
`probeStage: "actual"`, `rolledBack: true`, and `probe.actualBytes`; the
transaction is rolled back and the operation-specific `safeState` identifies
the retained state. This actual-size check catches bad planner estimates, but
it runs after population and indexing: it is not a filesystem quota and cannot
prevent transient relation, index, or WAL growth. Deployments requiring a hard
disk boundary must also put the derived workload on quota-constrained storage.

The dashboard submits create, replace/convert, and materialized refresh work
with `"background": true`. The API responds with `202 Accepted`, an operation
record, and a `statusUrl`; the dashboard polls that durable operation until the
database transaction has committed and the output checks have passed or a
terminal error is recorded. Closing the browser or an HTTP proxy timing out
does not cancel the PostgreSQL work. A service restart cannot preserve an
in-flight database connection: startup recovery marks such an operation
indeterminate with `failurePhase: "service-recovery"`; it does not infer an
unchanged target merely from the operation's failed or interrupted status.

Expected query and materialization guard failures recorded by a durable job
carry the same code, category where applicable, user guidance, reasons, probe,
operation, and known-state fields under `operation.error` as the synchronous
HTTP response; the stored error additionally records its HTTP `status` and
exception `type`.
Clients should surface that nested `userMessage`, stable derived-layer code,
and `suggestedAction` instead of replacing them with a generic background-job
failure. An unexpected preflight failure or failure followed by proven rollback
can use `code: "derived_layer.operation_failed"` with authoritative unchanged
state. An unexpected commit, rollback-finalization, or result-recording failure
uses that code with an `indeterminate` operation and inspection guidance; it
omits `stateUnchanged` and `safeState`, so inspect the operation, managed layer,
and catalog before retrying.

Failure phases are closed and machine-readable: `preflight` means no mutation
transaction began; `database-transaction` plus `rolledBack: true` means the
transaction body failed and an explicit rollback completed; failed rollback or
commit confirmation uses `transaction-rollback` or `transaction-commit` and is
indeterminate; `result-reporting` means the database mutation returned but its
durable result could not be recorded. A client that loses the initial mutation
response uses `request-response`, a client that loses observation of an
accepted operation uses `operation-polling`, and startup recovery uses
`service-recovery`. Only `preflight` and proven rollback responses may include
`stateUnchanged` and `safeState`.

The configuration service admits one active derived background job by default.
`DERIVED_MAX_BACKGROUND_JOBS` may be set from 1 through 4, but one is recommended
because derived mutations are serialized at the database boundary. When all
slots are occupied, a new background request is not queued: it returns HTTP
`429` with `code: "derived_layer.background_capacity"`, `blocked: true`,
`retryable: true`, and the active and maximum job counts. Wait for the recorded
operation to finish and submit the same reviewed request again.

For compatibility, callers that omit `background` retain the synchronous
response. Create, replace, and refresh work remains bounded by a 30-minute
database statement timeout; operations which swap relations also retain a
5-second lock timeout.

DDL, DML, session changes, notifications, copying, and dependencies on another
managed derived layer are rejected. PostgreSQL, PostGIS, and H3 functions used
by the query are not relation sources and do not need semantic profiles. The
AST, catalog, planner, and database resource ceilings are layered admission
controls; they cannot prove every data-dependent algorithm is cheap. Keep
`derive` restricted to trusted operators and review query cost before creation.

## H3 support and example

The bundled image builds
[`h3-pg` v4.2.3](https://github.com/postgis/h3-pg/tree/a26630b8353d441e6bc8065c0a8dcaa3d89ef87b)
from its pinned full commit and installs `h3` and `h3_postgis`. H3 PostGIS
functions expect EPSG:4326 longitude/latitude and do not reproject input.
PostgreSQL narrows the search path while refreshing a materialized view. This
H3 version's polygon SQL wrappers resolve nested PostGIS calls only when their
bodies run. Bundled initialization and `./bin/mapp upgrade-derived` therefore
catalog-check the four regular/experimental geometry/geography overloads as
members of `h3_postgis` before pinning their routine path to `pg_catalog,
public`. External operators must apply the equivalent administrator-owned
setting described in the external PostgreSQL handoff; the service never alters
extension-owned routines.

The service supplies `_mapp_h3_scope(geom_4326)` from the saved map envelopes.
Use it as the direct polygon input when generating output cells. This example
generates bounded resolution-9 candidates, then aggregates against the complete
declared source relation; it does not estimate metrics from a sampled subset:

```sql
WITH candidate_ids AS (
  SELECT DISTINCT generated.cell AS h3
  FROM _mapp_h3_scope
  CROSS JOIN LATERAL h3_polygon_to_cells(
    _mapp_h3_scope.geom_4326,
    9
  ) AS generated(cell)
),
candidate_cells AS (
  SELECT
    h3,
    h3_cell_to_boundary_geometry(h3) AS geom_4326
  FROM candidate_ids
),
projected_cells AS (
  SELECT
    h3,
    public.ST_Transform(geom_4326, 3857)
      ::public.geometry(Polygon, 3857) AS geom_3857
  FROM candidate_cells
)
SELECT
  cell.h3::text AS h3_id,
  count(path.*)::bigint AS path_count,
  sum(ST_Length(path.geom_3857)) AS path_length_m,
  cell.geom_3857
FROM projected_cells AS cell
JOIN leeds.definitive_paths AS path
  ON path.geom_3857 && cell.geom_3857
 AND ST_Intersects(path.geom_3857, cell.geom_3857)
GROUP BY cell.h3, cell.geom_3857
```

Declare `leeds.definitive_paths` as the source, `h3_id` as the ID, and
`geom_3857` as geometry. The map envelope bounds candidate-cell generation;
every candidate's values still use all intersecting rows in the complete source
relation.

The example uses the `public` namespace required by the supported derived-owner
provisioning. The guard does not trust that spelling alone: before PostgreSQL
analyzes the query, it proves that `public.geometry` is the installed PostGIS
type and that `public` is the extension's authoritative namespace. Qualifying
both the routine and output cast also makes the required type contract explicit:

```sql
public.ST_Transform(geom_4326, 3857)
  ::public.geometry(Polygon, 3857)
```

An external database must keep its PostGIS/H3 installation compatible with the
controlled `pg_catalog, public` search path described in provisioning. Qualified
cast admission does not broaden that path. The explicit typmod remains necessary
for derived-output validation.

`h3_polygon_to_cells` requires a literal resolution from 0 through 15 and the
direct `_mapp_h3_scope.geom_4326` argument. The service estimates scope cells
from spherical envelope area and H3's average cell area, applies a 1.5 safety
factor, and blocks estimates above 2,000,000 cells. `h3_grid_disk` and
`h3_grid_ring` require a literal distance no greater than 25. Non-expanding H3
index, parent, and boundary functions remain available. Immediate
`h3_cell_to_children(cell)` expansion (including an explicitly provable current
resolution plus one) is allowed; arbitrary child targets, uncompact operations,
and grid-path expansion are rejected because their size is not locally bounded.
The guard also composes these operations: each disk uses its maximum
`3k(k+1)+1` cells, a ring uses at most `6k`, and immediate children multiply by
7. The combined scoped estimate must remain at or below 10,000,000 cells. The
PostgreSQL plan budget still applies after these H3-specific checks.

## API shared by dashboard and CLI

| Route | Scope | Purpose |
| --- | --- | --- |
| `GET /api/derived-layers/capabilities` | `inspect` | Modes, PostGIS/H3 versions, exact-overload catalog and bounded execution readiness, universal query-plan limits, generated-row-aware nested-loop pair planning, H3 bounds, and the materialized-size limit |
| `GET /api/derived-layers/map-extent?locale=KEY` | `inspect` | Preview the server-resolved fixed workspace map extent |
| `GET /api/derived-layers` | `inspect` | Definitions without SQL |
| `GET /api/derived-layers/{name}` | `inspect` | One definition including SQL |
| `POST /api/derived-layers` | `derive` + `semantic:inspect` | Create an automatically map-scoped, compute-probed view or materialized view from ready semantic source profiles; materialized output is also size-probed; accepts optional `background` and locale selector in `spatialScope` |
| `POST /api/derived-layers/{name}/refresh` | `derive` | Confirmed, compute- and size-probed materialized refresh; accepts optional `background` |
| `POST /api/derived-layers/{name}/replace` | `derive` + `semantic:inspect` | Confirmed, automatically map-scoped atomic replacement or kind conversion; every query is compute-probed and materialization is size-probed |
| `POST /api/derived-layers/{name}/drop` | `derive` | Confirmed dependency-safe removal |

Both clients use these routes. They never receive the database credential and
do not duplicate server validation.
