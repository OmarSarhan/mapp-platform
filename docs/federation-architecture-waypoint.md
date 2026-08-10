# Federation architecture waypoint

## Status and purpose

This document is a north-star architecture and development handoff. It is not
the description of an implemented feature and does not change the current
single-database deployment contract.

The intended destination is a MAPP-owned spatial federation layer above one or
more independently operated source databases. MAPP should retain reviewed
knowledge about a source when that source is unavailable or replaced, combine
approved data from multiple sources, and optionally materialize bounded
results, without copying or modifying source data by default.

The implementation must be incremental. The first useful outcome is a safe,
non-invasive external read mode. Multi-source composition, materialization,
and scheduling follow only after source identity and trust boundaries are
sound.

Read this document together with the pages that own the contracts it extends:
[Architecture](architecture.md), [Managed derived
layers](derived-layers.md), [Semantic metadata control
plane](semantic-layer.md), [External PostgreSQL administrator
handoff](external-postgresql.md), [Security](security.md), [Backup and
restore](backup-restore.md), and [Repository split](repository-split.md). Where
this document and one of those pages disagree, the other page describes shipped
behavior and wins until the relevant waypoint is delivered and its page is
updated in the same change.

## What the platform already provides

The most common failure mode for this work would be to design a parallel
identity system beside one that already exists. Before proposing any new
concept, note what the current code already does.

| Existing capability | Evidence | Consequence for federation |
| --- | --- | --- |
| The configuration service holds **N database connections**, not one | `DB_CONNECTIONS` is built from every `DBS_*` environment variable in `config-ui/app.py:100` | Multi-database connectivity is not new work |
| Catalog discovery already **iterates every connection** | `discover()` in `config-ui/app.py:3739`; `GET /api/catalog` returns `databases` in `config-ui/app.py:5814` | Per-source relation discovery already exists |
| Workspace layers already **select a database** | `dbs` and `databaseKey` in `config-ui/schema/workspace.schema.json`; inheritance in `layer_db()` at `config-ui/app.py:3769`; XYZ resolves `DBS_<value>` | Layers can already bind to different databases |
| Semantic source identity is already a **3-tuple including the database** | `source_asset_id(alias, schema, relation)` at `config-ui/semantic_sources.py:176` hashes `postgresql\0alias\0schema\0relation` | Source-aware semantic identity is already delivered |
| Semantic discovery already does a **cross-alias keyset walk** | `discover_page()` at `config-ui/semantic_sources.py:516`, ordering by `(alias, schema, relation)` | Paged multi-source catalogs already work |
| The allowlist grammar is already **alias-qualified** | `SEMANTIC_SOURCE_ALLOWLIST=MAPP:leeds.*`, parsed at `config-ui/semantic_sources.py:104` | Per-source exposure policy already has a syntax |
| The layer drop guard is already **keyed by alias** | `public.mapp_platform_layer_dependencies (alias, relation)` in `docker/postgis/init/25-platform-layer-drop-guard.sql`; synced per alias at `config-ui/app.py:1406` | Per-source dependency protection already has a shape |
| Cross-system dependency reconciliation already uses a **compound key** | `f"{alias}:{schema}.{relation}"` at `config-ui/app.py:1339` | The compound identity string already exists |

The one place with **no** database dimension is precisely the place federation
needs it:

| Missing capability | Evidence |
| --- | --- |
| Managed derived layers use exactly **one** connection | `DERIVED_DATABASE_URL` is a single value; `config-ui/app.py:216` |
| Derived `sources` are bare `schema.relation` strings | `_relation()` at `config-ui/derived_layers.py:324`; stored as `text[]` at `config-ui/derived_layers.py:1486` |
| Dependency proof uses `pg_depend`, which cannot cross a database | `_dependencies()` at `config-ui/derived_layers.py:1655` |
| Derived layers use the **synthetic alias `"derived"`** as a placeholder | `config-ui/app.py:1382`, allowed at `config-ui/app.py:6035` |
| Derived semantic assets are random `uuid4` with an alias-less binding | `config-ui/derived_layers.py:3074` and `:1577` |

This reframes the whole programme. Federation is not "make MAPP
multi-database"; the read and metadata planes already are. Federation is
**"give the derived-layer execution plane the same source dimension the rest of
the platform already has, and give it a way to read relations that live in
another database."**

The synthetic alias `"derived"` at `config-ui/app.py:1382` is the federation
database in embryo. The target state makes it a real, registered database.

## Naming

The word *source* is already overloaded in this repository across four
established meanings:

| Existing usage | Means | Where |
| --- | --- | --- |
| `SEMANTIC_SOURCE_ALLOWLIST`, `semantic:source`, "source asset" | one **relation** registered in the semantic catalog | [Semantic metadata control plane](semantic-layer.md) |
| derived-layer `sources[]` | the **relation dependencies** of one derived query | [Managed derived layers](derived-layers.md) |
| `generated` vs `curated`, "source-owned facts" | the **lifecycle authority** over a fact | [Semantic metadata control plane](semantic-layer.md) |
| ETL "source", `--check-source` | an **upstream feed** such as Leeds ArcGIS or Nomis | `etl/README.md` |

Introducing a fifth meaning — *source* as an entire external database — will
produce ambiguous code, ambiguous error messages, and ambiguous API fields.

**Use the existing vocabulary.** A database is an **alias**, exactly as
`DBS_<ALIAS>`, the workspace `dbs` key, and the semantic binding already use
the term. Where prose needs to be explicit, write *source alias* for a
registered upstream database and *source relation* for one relation inside it.
Reserve `sourceId` for the case where the design deliberately decides that a
registered database needs an identity **separate from** its connection alias —
and if it does, state why in the same change.

This is not cosmetic. Renaming `alias` to `sourceId` inside the semantic
identity string at `config-ui/semantic_sources.py:177` changes **every existing
asset UUID**, and `semantic-service/semantic_store.py:912` rejects a new
generation whose `binding` is not canonically equal to its predecessor's. Any
such rename is a migration and rebind exercise, not an edit.

## Architectural decision

MAPP should eventually use one dedicated **federation database** as its sole
spatial execution database. Existing PostgreSQL/PostGIS databases become
independently registered, read-only **source aliases**. PostgreSQL FDW is the
first transport, not the platform-level abstraction.

```text
                       MAPP control plane
                +-----------------------------+
                | semantic catalog            |
                | proposals and audit         |
                | bounded verifier            |
                +--------------+--------------+
                               |
                               v
                   MAPP federation database
                +-----------------------------+
                | alias registry               |
                | observations and history      |
                | source_<alias> foreign tables |
                | integration views             |
                | derived_layers outputs        |
                | optional materializations     |
                | PostGIS/H3 and query guards   |
                +---------+-----------+-------+
                          |           |
                    read-only FDW  read-only FDW
                          |           |
                          v           v
                    source DB A  source DB B
```

Only the federation database receives MAPP-owned extensions, schemas,
definitions, dependency guards, event triggers, derived output, or spatial
execution state. A source database receives only operator-approved read
credentials and grants. MAPP does not install its bundled upgrade into a
source database.

This distinction matters because the current bundled database upgrade can
install H3, create and alter roles, create `derived_layers`, prepare source
indexes, place guard objects in `public`, and install a database-wide event
trigger — see `docker/postgis/init/05-h3.sql`,
`docker/postgis/init/10-roles.sh`,
`docker/postgis/init/25-platform-layer-drop-guard.sql`,
`docker/postgis/upgrade-derived.sh`, and
`docker/postgis/prepare-spatial-indexes.sh`. Those are appropriate only in a
database deliberately owned for MAPP execution.

### Relationship to the current single-database contract

Today `DBS_MAPP` and `DERIVED_DATABASE_URL` name the **same physical
database**, and `scripts/verify.sh:1883` enforces that:

```text
audit["databaseName"] != reader_session["database_name"]   -> fail
```

That invariant exists for a stated reason (`README.md`, "Database
configuration"): the dashboard must not validate against a different database
from the one XYZ reads. Federation does not get to delete it — it must
**generalize** it.

The correct generalized invariant is:

> The derived owner's database must be the database that the workspace's
> effective `dbs` alias resolves to.

Two consequences follow, and both are ordering constraints on delivery:

1. **The runtime reader and the derived owner must move to the federation
   database in the same step.** This applies specifically to the transition
   from `bundled`/`external` into `federated` mode (see **Deployment topology
   and database mode**): moving only the derived owner breaks
   `scripts/verify.sh:1883`; moving only the runtime reader breaks every
   managed derived layer. There is no safe intermediate release that moves one
   without the other.
2. **Within `federated` mode, zero registered aliases must be a valid,
   fully-functional state.** Given the decision to add a third mode value
   rather than reinterpret `bundled`/`external` as degenerate federations,
   existing deployments never need to become expressible as a federation at
   all — they simply stay on their current mode, untouched. The obligation
   this leaves is narrower but still real: a deployment that has just switched
   into `federated` mode, before registering a single alias, must behave
   identically to a single-database deployment. If it doesn't, switching the
   mode is itself a breaking change, which the "does not change the current
   deployment contract" promise at the top of this page does not permit.

Design for (2) first. If `federated` mode cannot describe a plain
single-database deployment before any alias is registered, the model is
wrong.

## Classifying current database mutations

The first task this document recommends is to inventory every database mutation
and classify it. That inventory has now been taken, and it is recorded here so
the implementation team starts from evidence rather than repeating the survey.

### Source-owned — but mutated by MAPP today

These are the sites that make the safety argument for federation concrete. Each
one reaches into a data-source schema, and each one must stop doing so before
any externally operated database is registered.

| Site | Mutation |
| --- | --- |
| `docker/postgis/init/10-roles.sh:59` | `CREATE SCHEMA leeds AUTHORIZATION <etl role>` |
| `docker/postgis/init/10-roles.sh:60`, `:67` | `GRANT USAGE ON SCHEMA leeds`; `ALTER DEFAULT PRIVILEGES FOR ROLE <etl> IN SCHEMA leeds` |
| `docker/postgis/upgrade-derived.sh:116` | `GRANT SELECT ON ALL TABLES IN SCHEMA leeds` |
| `docker/postgis/upgrade-derived.sh:120` | `REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA leeds` |
| `docker/postgis/upgrade-derived.sh:136` | `ALTER DEFAULT PRIVILEGES FOR ROLE <etl> IN SCHEMA leeds` |
| `docker/postgis/prepare-spatial-indexes.sh:69`, `:221`, `:252` | `CREATE INDEX` and `ANALYZE` **inside the source schema** |
| `config-ui/semantic_sources.py:602`, `:910` | `LOCK TABLE <source relation> IN ACCESS SHARE MODE` — not a data mutation, but a server-side effect on a source relation |
| `etl/src/leeds_arcgis_etl/database.py`, `census_database.py` | Legitimately source-owned DDL/DML, but currently executed by MAPP's bundled ETL against the execution database |

### Federation-owned

| Site | Mutation |
| --- | --- |
| `docker/postgis/init/10-roles.sh:63`, `upgrade-derived.sh:123` | `CREATE SCHEMA derived_layers` and its ACLs |
| `docker/postgis/init/05-h3.sql`, `upgrade-derived.sh:21` | `CREATE EXTENSION h3` / `h3_postgis`, and the four-wrapper `search_path` hardening |
| `config-ui/derived_layers.py:1478`–`:1624` | `_definitions`, `_semantic_outbox`, `_maintenance` bootstrap DDL |
| `config-ui/derived_layers.py:2819`, `:3008`, `:3015`, `:3034`, `:3149`, `:3225`, `:3303`, `:3415` | Probe view, create, materialize, refresh, replace-swap, and drop |
| `config-ui/derived_layers.py:2017`, `:2099`, `:2122` | Unique ID index and up to five spatial GiST indexes |
| `config-ui/derived_layers.py:3050`, `:3288` | `GRANT SELECT` to the runtime reader role |
| `docker/postgis/prepare-spatial-indexes.sh` restricted to `derived_layers` | Index preparation for managed output only |

### Control-plane-owned

| Site | Mutation |
| --- | --- |
| `docker/postgis/init/25-platform-layer-drop-guard.sql`, `upgrade-derived.sh:143`–`:242` | Guard table, `SECURITY DEFINER` sync function, and a **database-wide** `ddl_command_end` event trigger, all in shared `public` |
| `docker/postgis/init/20-runtime-info.sql` | `public.instance_runtime` |
| `docker/postgis/init/10-roles.sh:23`–`:51`, `upgrade-derived.sh:72`–`:107` | Role creation and cluster-scoped role GUCs |
| `config-ui/derived_layers.py:1866`, `:3494`, `:3877`, `:3936`, `:4202` | Outbox enqueue/claim/archive and the reset maintenance gate |

### Two findings that contradict the target invariants

**MAPP writes into a source database through its read-only credential.**
`sync_layer_dependency_guard()` at `config-ui/app.py:1406` opens a connection
for **each workspace alias** using that alias's `DBS_<ALIAS>` credential and
calls `public.mapp_sync_platform_layer_dependencies(alias, relations)` at
`config-ui/app.py:1426`. Because that function is `SECURITY DEFINER`, the
nominally read-only runtime role effects a `DELETE` plus `INSERT` on a guard
table in the source database's `public` schema. It is called on workspace save
and proposal apply (`config-ui/app.py:2821`, `:3604`) and wrapped in a bare
`except Exception: continue` at `config-ui/app.py:1429`, so it is deliberately
fail-open.

This is the **only** write through any `DBS_*` connection in the platform, and
it is the direct counter-example to this document's first desired property:
"source databases remain authoritative and unchanged by normal MAPP use." As
written, registering a second alias would cause MAPP to attempt guard writes
into an externally operated database, and to silently continue when they fail.

**Decided:** the dependency guard moves entirely into the federation database.
No configuration keeps a write path into a source database — the guard tracks
dependencies on federation-visible relations (including foreign tables)
instead, and its current fail-open behavior at `config-ui/app.py:1429` is
removed outright rather than made conditional.

**The bundled upgrade resets a role password on every start.**
`docker/postgis/upgrade-derived.sh:82` runs an unconditional
`ALTER ROLE <derived> LOGIN PASSWORD ...` from `.env`, and
`ensure_bundled_database_upgraded()` at `bin/mapp:403` invokes that script
before `up`, `serve`, `config-ui`, `etl`, `census-etl`, and `all`. That is
correct for a database MAPP owns and catastrophic against one it does not. Any
federation credential path must be structurally incapable of reaching a source
database with this pattern — which is a stronger requirement than "do not run
the bundled upgrade externally," because the guard today is a mode check in the
wrapper rather than a property of the code.

## Desired properties

The completed architecture should provide all of the following:

- source databases remain authoritative and unchanged by normal MAPP use;
- ordinary federation does not replicate source rows;
- source identity, generated facts, curated meaning, observations, and history
  survive temporary disconnection;
- remembered metadata is never presented as proof that a source is currently
  available or unchanged;
- multiple source databases can contribute to one reviewed integration view;
- workspaces bind to stable federation relations rather than connection
  strings or ambiguous remote table names;
- optional materializations are explicitly identified as local copies, with
  recorded provenance and freshness;
- source ETL, source registration, integration publication, materialized
  refresh, and workspace application remain separate lifecycle operations;
- existing semantic, SQL-shape, H3, plan, resource, proposal, preview, reload,
  and audit protections remain in force;
- every detected drift — structural, credential, physical-identity, or
  cross-source consistency — is surfaced to an operator, never merely logged,
  and paired with a concrete suggested resolution path rather than a bare
  status flag.

## Non-goals and constraints

The first implementation does not need to provide:

- arbitrary heterogeneous database connectors;
- automatic conflict resolution between semantically different datasets;
- transparent failover to a similarly named relation;
- continuous change-data capture;
- incremental materialized refresh;
- writes through foreign tables;
- executable connector upload or discovery-time code import;
- guaranteed data freshness where a source supplies no reliable version
  signal;
- verification of data license compatibility, attribution requirements, or
  re-identification/PII risk when combining sources — the platform records a
  registration-time acknowledgement (see Source lifecycle, "Register") but
  resolving these is a business process the registering operator owns outside
  this system.

PostgreSQL databases are isolated from one another. A separate support database
cannot query another database without a transport such as `postgres_fdw`.
PostGIS functions must exist in the database where spatial expressions execute.
FDW pushdown is not guaranteed, so every spatial and cross-source query still
needs bounded planning and representative performance evidence.

**Indexes are never shared across the FDW boundary — PostgreSQL has no
mechanism for this.** A foreign table has no local storage and therefore
cannot itself be indexed; `CREATE INDEX` on a foreign table is rejected
outright. What actually makes a federated query fast is that a predicate gets
pushed down into the SQL text sent to the source, and the *source's own*
native index — already required of every registered relation (see ETL
ownership's "native and required expression spatial indexes") — is used by
the *source's* planner to satisfy it there. Two things govern whether that
happens: whether the predicate's shape is push-down-eligible at all, and
whether the alias's extension versions are aligned with the federation
database's (see extension-version diffing under **Freshness and
verification**). Neither is index sharing; both are prerequisites for the
source doing its own indexed lookup instead of a full remote scan.

Two constraints are load-bearing enough to state as explicit accepted losses,
because the current documentation promises behavior that FDW cannot preserve:

**Per-user row-level security is lost across FDW.** A foreign table reaches the
remote database through one user mapping, so every MAPP caller presents the
same remote identity. [External PostgreSQL administrator
handoff](external-postgresql.md) currently tells administrators that source RLS
"remains authoritative" and must be verified "for the remote login" — that
stays true, but under federation it means RLS is evaluated once, for the mapped
role, identically for every MAPP user. Any deployment relying on per-user RLS
at the source must be told this explicitly before it registers an alias.

**Planner-estimate admission control weakens across FDW.** The entire
derived-layer safety model rests on `EXPLAIN` estimates — total cost, node
rows, plan depth, join fan-out, nested-loop pair work, and materialized size
(see the probe tables in [Managed derived layers](derived-layers.md)). For a
foreign table those estimates come either from `use_remote_estimate` (an extra
remote planning round trip) or from local statistics that `ANALYZE` on the
foreign table last collected. Neither is as trustworthy as a local relation's
statistics. The design must therefore state a position, not inherit one
silently:

- whether `use_remote_estimate` is required per alias, and who pays its latency;
- what `fetch_size` and per-alias row ceiling apply;
- what remote-side `statement_timeout` the mapped role carries, since the local
  timeouts in `docker/postgis/init/10-roles.sh` do not bound remote work.

**Cross-source read consistency is lost across independent foreign servers.**
Each foreign server connection observes its own remote snapshot; PostgreSQL
provides no cross-database MVCC. An integration view joining source A and
source B under concurrent writes can combine A's state at one instant with
B's state at a slightly different one, producing a result that never existed
at either source alone. This is the same class of loss as the two above and
must be stated rather than discovered in production.

The backend should actively watch for this rather than only document it: when
planning or refreshing a multi-source integration, record each contributing
source's observation/read timestamp alongside the result, and surface a
warning — not a silent pass — when a query would materially combine sources
whose freshness evidence disagrees by more than the alias's declared
`freshnessStrategy` tolerance. The point is a suggested resolution path
(re-verify before publish, narrow the query to a consistent window, or
explicitly accept and record the skew), not a block, since PostgreSQL cannot
guarantee the alternative.

## Logical model

The public model should describe aliases and their relations, not FDW server
objects. That preserves a route to future connector types without changing
semantic and workspace identity again.

### Source alias

A source registration should include at least:

| Field | Meaning |
| --- | --- |
| `alias` | Stable MAPP-assigned identity matching the existing `DBS_<ALIAS>` / workspace `dbs` contract, never inferred from a hostname |
| `displayName` | Reviewed operator-facing name |
| `kind` | Initially `postgresql`; transport implementation remains internal |
| `connectionRef` | Reference to a secret, never the connection string itself |
| `allowedRelations` | Exact approved remote relation allowlist |
| `status` | `pending` (registered, awaiting Approve exposure — TTL-bound, auto-expires), `active`, `unavailable`, or `retired`; only `pending` and `active` count against the alias cap |
| `freshnessStrategy` | `manual`, `maximumAge`, `timestampColumn`, or `versionRelation` |
| `physicalIdentity` | Observed database/server identity used to detect replacement |
| `lastObservation` | Latest bounded connectivity, schema, and version evidence |
| `registeredBy` | Principal that created the registration — dashboard user identity or CLI credential identity, and which scope was used; minimal viable attribution for audit, not itself a permissions boundary |
| `approvedBy` / `approvedAt` | Principal and timestamp of Approve exposure — together with `allowedRelations`, this triple is the consent record (see **Source lifecycle > 3. Approve exposure**) |

The alias contract is already fixed by existing code, and the two places that
define it **already disagree**:

| Definition | Pattern | Leading digit | Underscore | Length bound |
| --- | --- | --- | --- | --- |
| `DB_KEY` at `config-ui/workspace_schema.py:21`, mirrored as `databaseKey` in `config-ui/schema/workspace.schema.json` | `^[A-Za-z0-9-]+$` | permitted | rejected | **none** |
| Allowlist parser at `config-ui/semantic_sources.py:113` | `[A-Za-z][A-Za-z0-9_-]{0,62}` | rejected | permitted | 63 |

An alias such as `9-council` is a valid workspace `dbs` key that the semantic
allowlist cannot express; an alias such as `council_prod` is the reverse.
Reconciling these is a prerequisite, not a detail, because the alias becomes a
generated PostgreSQL schema name — and the unbounded `DB_KEY` length is the
sharper problem, since `source_` plus an alias must stay within PostgreSQL's
63-byte identifier limit.

**Decided:** the semantic allowlist pattern (`[A-Za-z][A-Za-z0-9_-]{0,62}`)
becomes the single alias grammar. `DB_KEY` at `config-ui/workspace_schema.py:21`
and `workspace.schema.json`'s `databaseKey` must be tightened to match — this
narrows what a workspace `dbs` key may be, so any existing deployment with an
alias outside the new pattern (leading digit, over 63 characters) needs an
explicit migration note in the change that lands this, not a silent
validation break.

Aliases must also not expose credentials, hostnames, or tenant-sensitive values
in generated schema names.

### Source relation

A registered relation is identified by the tuple:

```text
(alias, remote schema, remote relation)
```

This is already the semantic identity at `config-ui/semantic_sources.py:176`.
`transport.bus_stops` in two databases therefore already represents two
different assets, and no new identity concept is required for the metadata
plane.

A relation record should retain:

- its remote and federation bindings;
- relation kind;
- generated columns and database types;
- stable identifier evidence;
- geometry/geography type and positive SRID;
- structural fingerprint and observation time;
- source publication/version evidence when available;
- semantic asset identity and generation;
- lifecycle state and dependency information.

### Relation strings are currently un-parsed

Six call sites re-parse a relation string ad hoc and default a missing schema
to `public`: `config-ui/app.py:1298`, `config-ui/app.py:4419`,
`config-ui/app.py:4613`, `config-ui/control_api.py:3082`,
`config-ui/derived_layers.py:324`, and the catalog composite key at
`config-ui/app.py:4326`. The workspace `RELATION` regex at
`config-ui/workspace_schema.py:27` permits at most one dot, so an
alias-qualified relation is currently a schema violation.

Before adding a source dimension, consolidate relation parsing into one
function with one contract. Threading a third component through six
independently written `split(".")` sites is where this project will acquire its
first silent mis-binding.

**Decided:** consolidate directly into the eventual `(alias, schema, relation)`
shape, not a two-part `schema.relation` contract extended later. With only one
alias ("MAPP") registered today, the alias component is a constant everywhere
this new function is called — but the six call sites, the workspace `RELATION`
regex, and every already-written workspace layer string only get touched
once. Extending a two-part contract later would touch the same six sites
again and force a migration of workspace layers written in the interim;
building the three-part shape now costs one extra field with nothing behind
it yet.

Two existing gaps in that area should be closed in the same pass:

- `validate_catalog()` at `config-ui/app.py:4324` validates `layer["table"]`
  but **not** the zoom-keyed `layer["tables"]` map, which receives structural
  validation only at `config-ui/workspace_schema.py:656`.
- The area-weighted H3 recipe validates the semantic binding by
  `(schema, relation)` only at `config-ui/derived_layers.py:767`, accepts
  `binding.alias` as unvalidated pass-through metadata at
  `config-ui/derived_layers.py:772`, and then drops it when building
  `createRequest["sources"]` at `config-ui/derived_layers.py:928`. Today that
  is harmless because one alias exists. With two aliases holding a relation of
  the same name, the recipe would resolve the wrong one. This is a present-day
  latent defect and a cheap, self-contained federation prerequisite.

### Observation versus truth

Stored catalog information is an observation, not a live guarantee. The API
and UI must report connectivity, schema compatibility, data-version evidence,
and materialization age independently.

```text
connectivity:          reachable | unavailable | unknown
schema:                current | changed | unknown
source freshness:      current | possibly_stale | stale | unknown
last connected:        timestamp or null
last schema verified:  timestamp or null
source version:        opaque value or null
```

The same table name at a new physical database is not the same source unless an
operator performs an explicit, evidenced rebind.

This is the same distinction the semantic layer already draws between
`generated` and `curated` facts, and it should reuse that machinery rather than
inventing a second history store. An observation is a source-owned fact; it
belongs on the `generated` side, changes only through a trusted lifecycle
event, and must never be writable by a curated proposal.

## Federation database layout

Use four classes of schema with different ownership and publication rules.

### Control schema

The alias registry and observation history (decision #3) live here, in the
federation database itself — not in control-plane state, not in semantic
state. This schema holds the alias records (identity, `connectionRef`,
`allowedRelations`, status, `registeredBy`/`approvedBy` attribution) and their
observation/freshness history. It is written only by the registration and
provisioning pathways and by the verifier; nothing here is a foreign table,
and nothing here is workspace-visible. Placing it in the federation database
is what keeps registry, observation, integration, and derived state under one
backup/restore boundary — see **Backup and recovery implications**.

### Foreign source schemas

Each alias is represented by an isolated, generated schema such as
`source_council_prod`. It contains only explicitly approved foreign tables.

- Do not perform an unrestricted production `IMPORT FOREIGN SCHEMA`.
- Import or define only allowlisted relations and required columns.
- Treat foreign schemas as read-only infrastructure.
- Do not let workspace proposals create, alter, or drop FDW objects.
- Do not grant foreign-server or user-mapping administration to runtime or
  derived roles.

**Decided:** a workspace layer may never bind directly to a foreign table,
even for a trivial single-source pass-through. Every workspace-visible
relation is an integration or derived relation, from day one — there is no
transitional direct-binding mode. This removes a code path rather than adding
one: workspace resolution only ever needs to know about integration/derived
schemas, never about `source_<alias>` schemas at all.

### Integration schemas

Reviewed integration views provide stable data products above source-specific
bindings. They may rename fields, normalize identifiers and geometry, align
classifications, add provenance, and combine compatible sources.

Every multi-source product should expose sufficient provenance to identify its
origin, normally including a controlled alias and a source-stable feature
identifier. Identifier collision rules must be explicit; concatenating
unbounded or untrusted values is not an acceptable implicit convention.

Integration creation and replacement must declare direct source relations,
resolve ready semantic profiles, and pass the same universal SQL-shape,
bounded-H3, recursive-plan, and PostgreSQL-plan protections as managed derived
queries. It must not push a map predicate below a user aggregate or replace a
complete input with a sample.

### Derived output schema

`derived_layers` remains the home for task-specific calculations, H3
aggregation, and optional materialized output. Integration relations describe
stable products; derived relations answer particular analytical or display
needs. The two lifecycles should not be conflated.

Note what moves with `derived_layers`: it also contains the private
`_definitions` registry and `_semantic_outbox`, and the outbox is the atomic
PostgreSQL-to-SQLite bridge described in [Semantic metadata control
plane](semantic-layer.md). Definitions and their outbox events are committed in
one transaction, so the outbox must live in the same database as the
definitions. Relocating `derived_layers` therefore relocates the semantic
outbox to the federation database, and every backup, restore, reset, and
recovery-point statement in [Backup and restore](backup-restore.md) shifts with
it.

## The derived-layer query guard and foreign tables

This is the largest unaddressed technical risk in the programme and needs its
own design pass before any FDW object exists.

`config-ui/derived_query_guard.py` is 1,888 lines of catalog-provenance
enforcement. It proves that every routine, operator, type, and cast a submitted
query depends on is either a genuine `pg_catalog` builtin or a member of the
approved `postgis`, `h3`, or `h3_postgis` extensions, by walking `pg_depend`
from the transient view's `pg_rewrite` entry. It contains **no occurrence** of
`foreign`, `fdw`, `srvname`, or `pg_foreign_table`.

That matters in three specific ways.

**1. The guard checks routine provenance, never relation kind.** A foreign
table is, to the guard, an ordinary relation. It would pass every existing
check while silently executing a remote query through a foreign-data-wrapper
handler — a code path the guard was written to exclude for every other object
class. Foreign tables must become a first-class provenance class: resolved
through `pg_foreign_table` and `pg_foreign_server`, required to belong to a
MAPP-provisioned server, and rejected when their FDW handler is anything other
than the approved `postgres_fdw`.

**2. The relation-schema denylist is a denylist.** `FORBIDDEN_RELATION_SCHEMAS`
at `config-ui/derived_query_guard.py:17` contains only `derived_layers` and
`information_schema`. Generated `source_<alias>` schemas would be freely
readable by any submitted derived query with no way to distinguish an approved
foreign table from an unapproved one.

**Decided:** invert to an allowlist. Only integration and derived schemas
(plus named system schemas already required for query execution) are readable
by a submitted derived query; every `source_<alias>` schema is denied by
default, consistent with workspaces never binding directly to a foreign table.
A newly generated source schema is therefore unreachable from any derived
query the moment it's created, not exposed until someone remembers to deny
it.

**3. Dependency proof cannot cross a database.** `_dependencies()` at
`config-ui/derived_layers.py:1655` reads `pg_depend`, and the exact-match
assertion `dependencies != definition["sources"]` appears four times
(`config-ui/derived_layers.py:2824`, `:3021`, `:3137`, `:3238`). Under
federation the resolved dependency is the **local foreign table**
(`source_leeds.bus_stops`), while the meaningful declared source is the
**remote relation** (`LEEDS:leeds.bus_stops`). The mapping between the two must
be recorded by the provisioner and proved at mutation time, or the declaration
becomes unverifiable — removing the strongest existing guarantee that a derived
layer reads only what it declared.

There is one encouraging detail: `_dependencies()` at
`config-ui/derived_layers.py:1667` **already** admits relkind `'f'`, while
catalog discovery (`config-ui/app.py:3714`) and semantic sync
(`config-ui/semantic_sources.py:53`) do not. The system is therefore currently
fail-closed — a foreign table can be detected as a dependency but can never
obtain the ready semantic profile that create requires. That asymmetry is the
exact seam federation opens, and closing it deliberately is the first slice
recommended at the end of this document.

Finally, PostGIS and PROJ versions may differ between the federation database
and each source. Execution happens in the federation database, so source
versions matter only for pushdown — but an `ST_Transform` pushed to a source
with a different PROJ release can return different coordinates from the same
expression evaluated locally. Record each alias's PostGIS/PROJ version as
observation evidence.

**Decided:** allow pushdown of a geometry transform only when the alias's
observed PROJ version exactly matches the federation database's; otherwise
force local execution. The actual lever is `postgres_fdw`'s per-server
`extensions` option: the FDW provisioner includes `postgis` in that option
only when Discover/Observe evidence shows the alias's PostGIS/PROJ/GEOS
versions matching the federation database's, and omits or removes it
otherwise. This is a **reprovisioning-time** decision, not a live per-query
toggle — `extensions` is a static, all-or-nothing setting for the whole
foreign server, so a version drift detected after provisioning downgrades the
alias to "pushdown disabled, needs reprovisioning" rather than instantaneously
changing behavior mid-query. Reprovisioning runs through the same
`federation:provision`-scoped path as the original Approve exposure,
triggered by the drift observation rather than automatically.

## Roles and trust boundaries

The target role model separates these authorities:

| Authority | Permitted responsibility |
| --- | --- |
| Source owner | Own and publish source data; outside MAPP control |
| Remote source reader | `CONNECT`, schema `USAGE`, and `SELECT` only on approved source relations |
| FDW provisioner | Create reviewed federation server, mapping, schema, and foreign-table objects |
| Federation runtime reader | Read approved integration and derived outputs for XYZ and catalog validation |
| Federation derived owner | Manage only controlled integration/derived objects under existing resource ceilings |
| Control-plane verifier | Perform bounded metadata/version observations without source writes |

No application role may be a source owner, superuser, database owner, member of
a privileged reachable role, or hold `BYPASSRLS`. Source row-level security
remains authoritative for the mapped remote login, subject to the per-user
limitation recorded under **Non-goals and constraints**.

Keep the existing derived-owner and runtime-reader connection, memory, spill,
parallelism, timeout, search-path, and effective-session checks. Add bounded
per-alias connection and background-admission limits so a federation cannot
exhaust a remote system.

Bound the aggregate too: a platform-wide ceiling on the number of
simultaneously registered aliases, defaulting to **50** and overridable via an
environment value (for example `MAPP_FEDERATION_MAX_ALIASES`), enforced at
registration regardless of whether the caller is the dashboard session or a
CLI credential. Per-alias limits bound what one remote system can suffer; this
bounds how many remote systems and foreign-server connections the federation
database itself can be pushed to hold against `max_connections`. Raise the
ceiling by redeploying with a new value, not through a self-service runtime
API — the cap exists to be outside the reach of the same credential it
constrains.

**Decided:** the count-and-insert check runs under a serializing transaction
(or equivalent lock) so two concurrent registrations can never both land on
slot 50. A registration that reaches Approve exposure has a fixed TTL — it
auto-expires and frees its slot if never approved, so the cap, whose entire
purpose is to bound real connection resources, cannot be exhausted by
resource-free, never-approved registrations. A **retired** alias never counts
toward the ceiling, whether or not it retains archived state (see **Source
lifecycle > 8. Drift and retirement**); only `active` and pending-approval
registrations do.

### API scopes, not only database roles

The table above enumerates PostgreSQL roles. Every other capability in this
platform is also gated by an explicit, non-hierarchical API scope — `derive`,
`semantic:source`, `semantic:data`, `semantic:admin`, and the rest, as set out
in [Security](security.md). Federation introduces the most dangerous capability
the platform has ever had: registering an alias causes a MAPP-controlled
database to open an outbound authenticated connection to an operator-supplied
host. That must not be reachable by any existing scope through convenience.

The design must decide and record — settled here rather than left open:

- **Two distinct scopes, not one blanket `federation:admin`.**
  `federation:register` covers only creating an alias registration (Source
  lifecycle step 1, "Register") — a non-connecting intent record that proves
  nothing and reaches nothing. `federation:provision` covers **both** Discover
  (step 2) **and** creating the foreign server, user mapping, schema, and
  foreign tables (step 3, "Approve exposure").
- **Decided: Discover requires `federation:provision`, not `federation:register`
  alone.** Discover is the step that actually opens a live, credentialed
  outbound connection to the operator-supplied host — the capability this
  section calls the platform's most dangerous. Gating Discover behind
  `federation:register` would let one credential single-handedly exercise
  that capability, defeating the reason the scopes are split at all. This
  costs workflow fluidity — a register-only credential cannot itself see
  whether the host it registered is even reachable — which is the intended
  friction, not an oversight: a credential holding only `federation:register`
  can now connect to nothing, let alone self-approve, so a separately-granted
  credential (or a human) must always act before any outbound connection
  happens.
- **The dashboard session and CLI scopes are peer authorities, not a
  hierarchy.** Either the administrator dashboard session or a
  `mapp-config-cli` credential holding `federation:register` /
  `federation:provision` may perform these actions directly; neither is a
  prerequisite for the other. This keeps the platform's existing
  non-hierarchical scope model intact — the dashboard session is a separate
  authentication tier (as it already is for password changes and token
  issuance), not a scope that sits above `federation:*`.
- **CLI use from an unattended agentic harness is accepted, not a gap to
  close.** `federation:register` / `federation:provision` are ordinary
  bearer-token scopes and may be granted to a credential driven by an
  automated agent exactly as any other scope can. The two-step register/
  provision split above is the control that matters here, not a requirement
  that a human be present at the keyboard.
- **Decided: name the read-only scope `federation:observe`.** It covers
  observation status, freshness, provenance, and full topology (every alias
  plus its dependent relations) — passive reads only, never registration or
  provisioning. This is the scope the dashboard topology view and the CLI
  `federation status` command both require (see **Freshness and
  verification**); the Cross-repository impact table's scope-model row and
  decision #4 must both name it explicitly rather than leaving it implicit.
- how the new actions appear in `GET /api/capabilities`, since that route is
  the runtime authority for action IDs, risk classes, and conditional scopes.

## Credential, egress, and configuration boundary

FDW credentials must not appear in workspace JSON, semantic payloads,
proposals, audit details, screenshots, artifacts, logs, committed
configuration, or ordinary API responses. Three existing mechanisms constrain
how that is achieved.

**The environment-override guard is a literal enumeration.**
`reject_database_environment_overrides()` at `bin/mapp:121` and its verbatim
mirror at `scripts/verify.sh:29` list the database environment keys that a
shell export may not silently replace. A new `DBS_<ALIAS>` or federation
connection variable that is not added to **both** lists escapes the guard
entirely, letting an exported value redirect a source connection without
detection. Any new connection variable must be added in both places in the same
commit, and the enumeration should be replaced with a prefix rule.

**`doctor --add-missing` generates secrets by name.** `scripts/check_env.py`
substitutes a fresh `secrets.token_hex(24)` for any key whose name contains
`PASSWORD`, `SECRET`, or `TOKEN` and whose example value is blank or
`CHANGEME`. A federation credential variable named to match that pattern will
be auto-generated into `.env`, which is correct for a bundled test source and
actively wrong for a credential that must match a remote operator's database.
Name deliberately.

**There is no egress allowlist for database traffic.** The platform has a
strict, reviewed egress model: the browser runner has no direct outbound route
and reaches external assets only through the hostname-allowlisting Squid proxy
using the versioned `instance/browser-egress-allowlist.txt`; the semantic
service has no database network at all. Federation would let the federation
database open connections to arbitrary operator-supplied hosts with **no
equivalent reviewed input**. The design should decide whether registered alias
hosts belong in a versioned `instance` file subject to the same review
discipline, and which Compose network the federation database joins — today
every database-facing service sits on the non-internal `backend` network
(`compose.yaml:272`).

The design must also decide whether user mappings are provisioned exclusively
by a DBA or by a narrow privileged provisioning component. Runtime services
must not gain general foreign-server administration merely for convenience.

**Secret material must enter the system exactly once, through a write-only
path, regardless of caller.** Whether the dashboard session or a
`federation:register`-scoped CLI credential registers an alias, the
`connectionRef` on the Source alias record must be produced by a dedicated
secret-submission endpoint that accepts the raw credential, stores it in the
secret store, and returns only an opaque reference — never the value itself,
and never a value that a subsequent read of the alias or an audit log can
recover. This is standard practice for any system that lets a non-interactive
caller provision third-party credentials, and it is the only way the existing
cross-repo position — "whether the CLI ever handles a source connection
secret at all (it should not)" — survives CLI-driven registration: the CLI
transmits the secret once, on submission, and cannot read it back through any
API this platform exposes.

A write-only secret still needs to be checkable, so provide a **verify, not
read** path: an operator or CLI credential may resend the same credential
value to a verification endpoint, which compares it against the stored secret
(by hash comparison, or by attempting the bounded read-only connection already
used for discovery) and returns only a match/mismatch result and a
last-verified timestamp — never the stored value. This is how an operator
confirms "is the credential I think is registered actually what's stored"
without the platform ever supporting a read-back of secret material, and it
doubles as evidence for the `registeredBy` attribution: a successful verify
recorded against a specific principal is stronger proof of legitimate
possession than the attribution field alone.

A match/mismatch response is itself an oracle on a secret the caller may not
otherwise be entitled to, so it needs the same guard as the credential it
protects. **Decided:** this endpoint requires `federation:provision` — the
same scope already required to actually use the credential — and is
rate-limited per alias (for example, five mismatches per alias per hour),
with a distinct alert on repeated mismatches against the same alias, mirroring
the "alert distinctly" treatment already specified for a source rejecting
MAPP's own outbound credential (see **Source lifecycle > 8. Drift and
retirement**). No hard lockout: locking the endpoint after N failures would
let a guesser deny service to the legitimate credential holder, which is worse
than the oracle it would prevent.

## Deployment topology and database mode

`MAPP_DATABASE_MODE` is a strict two-value key — `bundled` or `external` —
validated independently in at least four places: `bin/mapp:342`,
`scripts/verify.sh:63`, `scripts/production_acceptance.py:105`, and the Compose
overlay selection that appends `compose.bundled-db.yaml`.

**Decided:** add a third value, `federated`. `bundled` and `external` are
untouched — every existing deployment stays exactly as it is, on the mode it
already runs, with zero migration obligation. Alias registration, FDW
provisioning, and cross-source composition are reachable only when
`MAPP_DATABASE_MODE=federated`. This trades the "no new mode" framing's
simplicity for a sharper guarantee: rather than requiring every existing
topology to be re-expressible as a degenerate case of the target, existing
topologies are simply out of scope for federation-specific code paths
entirely. The one invariant this still owes the rest of the document: within
`federated` mode, zero registered aliases must be a valid and fully
functional state, so turning the mode on is never itself a breaking change.

These single-database assumptions in the deployment surface are load-bearing
and must each be addressed for the new `federated` mode specifically —
`bundled` and `external` keep whatever behavior they have today and are
unaffected by any of this:

| Assumption | Location |
| --- | --- |
| External-mode URL validation reads exactly one Compose JSON path | `bin/mapp:380` (`services.xyz.environment.DBS_MAPP`) |
| Resolve-and-drift checks are per-variable, not per-alias | `scripts/verify.sh:132`, `:205` |
| Derived owner and runtime reader must share one physical database | `scripts/verify.sh:1883` |
| The identity audit assumes one pool and pins `current_user` to `XYZ_DB_USER` in bundled mode | `scripts/verify.sh:1033` |
| One `db` service, one `postgres_data` volume, one healthcheck | `compose.bundled-db.yaml:5` |
| `reset-data` reads exactly `volumes.postgres_data.name` and removes exactly one volume | `bin/mapp:584`, `:637` |
| Role and upgrade scripts assume one database with fixed schemas `leeds` and `derived_layers` | `docker/postgis/init/10-roles.sh`, `docker/postgis/upgrade-derived.sh` |
| The spatial-index helper hard-codes the schema pair | `docker/postgis/prepare-spatial-indexes.sh:69` |
| `DBS_MAPP` is injected with a required-guard at three points | `compose.yaml:19`, `:48`, `:101` |
| `./bin/mapp db` targets the single `db` service by name | `bin/mapp:743` |
| One hard-coded connection lookup remains in the service | `config-ui/app.py:604` (`DB_CONNECTIONS["MAPP"]`) |

Networking is the cheapest part. Everything database-facing already sits on
`backend`, which is not internal, so additional bundled databases join it and
external sources need no new network. `automation`, `semantic-control`, and
`browser-egress` are unaffected, and `compose.production.yaml` touches no
database setting.

## Source lifecycle

### 1. Register

An administrator creates an alias registration with a stable alias, secret
reference, TLS policy, exact relation allowlist, data-handling classification,
and optional freshness strategy. Registration records intent; it is not proof
of connectivity and does not expose relations.

The registering principal must explicitly acknowledge the data-handling
classification they recorded — including any licensing, attribution, or
personal-data implications of the relations they are about to allow — before
registration completes. The platform stores that acknowledgement for audit
but does not verify or adjudicate it; compliance is a business process outside
this system.

### 2. Discover

**Requires `federation:provision`** (see **API scopes, not only database
roles**) — this is the step that opens the live, credentialed outbound
connection, not Register.

The configuration service performs bounded, read-only discovery of:

- server and database identity;
- PostgreSQL, PostGIS, PROJ, and GEOS capability and version;
- accessible allowlisted relations and columns;
- identifiers, nullability, geometry type, and SRID;
- row-level-security behavior visible to the remote reader;
- planner statistics and relevant native spatial indexes;
- a configured scalar publication/version signal.

Discovery returns evidence and a focused candidate. It does not import every
accessible relation or write semantic state by itself. **Decided:** a source
with no allowlisted relations published yet (for example, a freshly created
database registered ahead of its first ETL run) is a valid Discover outcome,
not an error — the candidate is simply empty, and freshness reports `unknown`
per the existing `manual` strategy's "no automated claim" behavior. Approve
exposure may proceed against an empty candidate; a relation that did not exist
at approval time later appearing is itself a drift event (see **8. Drift and
retirement**).

This step should extend the existing bounded metadata read at
`config-ui/semantic_sources.py:580` rather than introducing a second discovery
path. That function already takes a repeatable-read read-only transaction and
an `ACCESS SHARE` lock, reads only catalog metadata, and fails closed on
privilege loss or concurrent change.

### 3. Approve exposure

After explicit review, the provisioning authority creates the foreign server,
restricted user mapping, isolated source schema, and exact foreign tables. A
revision or physical-identity change invalidates the candidate; do not silently
rebase it.

**Decided:** the same invalidation rule extends to the registration record
itself, not only the Discover-produced candidate — `connectionRef`,
`allowedRelations`, and TLS policy are version-stamped at Register, and
Approve exposure fails closed if the registration has been edited since the
principal who reviewed it last saw it. Without this, one `federation:register`
credential could edit an already-reviewed registration in the gap before a
separate `federation:provision` credential acts on it, silently provisioning
against relations or a credential nobody actually reviewed.

A registration that never reaches Approve exposure has a fixed TTL and
auto-expires, freeing its slot against the alias cap (see **Roles and trust
boundaries**).

For now, this recorded approval — the exact relation allowlist, the approving
principal, and the timestamp — is the consent record; no separate
source-operator consent system is needed at this stage. The Source alias
schema's `approvedBy`/`approvedAt` fields are what this record is persisted
in (see **Logical model > Source alias**).

### 4. Profile

The platform records generated source facts and a structural fingerprint, then
creates or updates a source-owned semantic generation. Curated annotations
remain proposal-controlled. A source observation must not erase reviewed
meaning, and a curator must not overwrite generated database facts.

### 5. Integrate

An operator proposes a stable integration view. Planning resolves source
profiles as the authority for relation and field meaning, validates provenance
and geometry, and applies resource guards. Creation does not add the result to
the workspace.

### 6. Publish

The operator creates a separate revision-bound workspace proposal. Candidate
preview verifies the actual integration relation, source states, structural
registration, map-layer visibility, interactions, and retained evidence before
apply and reload.

### 7. Observe

A bounded worker verifies sources on startup, before planning or refresh,
periodically at a conservative interval, and on explicit request. It records
observations; it does not rewrite integrations, refresh materializations, or
change the workspace automatically.

### 8. Drift and retirement

| Observation | Required response |
| --- | --- |
| Source unreachable (network/timeout) | Retain history; mark dependent live paths degraded; retry per backoff policy |
| Credential rejected (authentication failure) | Retain history; mark dependent live paths degraded; alert distinctly from an outage — this needs a MAPP-side secret rotation, not a wait, and must never be conflated with "unreachable" in status reporting |
| No structural change | Update observation evidence only |
| Previously-absent allowlisted relation appears | Treat as drift, not automatic Profile — record the observation and require an explicit Discover/Profile pass before the relation is usable |
| Compatible additive field | Record a new generation and offer reviewed reconciliation |
| Removed/type-changed field | Identify dependants and block affected publication or refresh |
| Geometry type or SRID change | Mark incompatible pending explicit review |
| Different physical database | Raise identity conflict; require explicit rebind |
| Source retired | Archive normal discovery while retaining exact-ID audit history, including the physical schema/server/mapping objects — nothing is dropped |

Follow the existing semantic archive contract: retirement is not deletion, and
normal collections omit archived assets while authorized exact-ID history
remains available.

**Decided:** retirement archives everything, including the live foreign
server, user mapping, and foreign tables — none of it is dropped, so the
exact-ID audit trail stays physically inspectable, not just metadata. This
means the `source_<alias>` schema name stays taken for as long as the retired
registration is archived, so re-registering the identical alias name requires
an explicit **reclaim** action (a new scope-gated operation, distinct from
ordinary Register) that an administrator invokes deliberately — it is never
implicit in submitting a new registration with the same name. Reclaim tears
down the archived physical objects for that name and only then allows a fresh
Register to succeed; the archived metadata/observation history is retained
under its own exact-ID audit entry regardless.

## Freshness and verification

Connectivity, structural compatibility, source data freshness, and cached
output freshness are separate facts. Never collapse them into one `healthy`
flag.

PostgreSQL has no universal, cheap table-content version. Support explicit
strategies with honest strength:

| Strategy | Evidence and limitation |
| --- | --- |
| `manual` | No automated claim about data freshness |
| `maximumAge` | Measures elapsed time, not whether data changed |
| `timestampColumn` | Bounded aggregate over an approved stored timestamp |
| `versionRelation` | One typed scalar from an approved source publication relation |
| Future CDC | Stronger change evidence, but invasive and operationally separate |

Custom arbitrary monitoring SQL should not be accepted. A version relation
must use a closed contract returning one bounded scalar. Numeric inspection
remains aggregate-only and source restrictions apply to every check.

The verifier should begin as a bounded worker in the control plane. Extract it
into a separate service only when source count, scheduling, availability, or
connection isolation justifies another deployable component. Its design must
already support finite timeouts, bounded concurrency, backoff, cancellation,
and per-source status so extraction does not change semantics.

Checking cannot be purely periodic. Freshness and structural change must also
be evaluated **on use** and **on change**, with drift propagated to whatever
depends on it:

- **On use** means consulting the latest recorded observation and its age
  before serving a request from a federated or integration relation — not a
  live remote round trip on every query, which would reintroduce the exact
  per-query latency and remote-load risk the bounded worker exists to avoid.
  If the recorded observation is older than the alias's configured cadence
  permits, the request proceeds against the (labelled) cached state and an
  async re-check is queued; it does not block.
- **On change** means the verifier reacts immediately when an observation
  detects drift, rather than waiting for the next periodic tick to be the
  first thing that notices.
- **Dependency flagging** means every drift observation resolves and marks
  every integration view, derived layer, and materialization that depends on
  the affected relation as degraded — reusing the existing layer-dependency
  guard's alias-keyed dependency tracking rather than inventing a second
  graph. An alias-level flag with no visible effect on its dependents is not
  sufficient; the point is that a workspace author sees *their* layer is
  affected, not just that some alias somewhere changed state.
- **Extension-version diffing** means every verification pass — not only the
  initial Discover step — compares the alias's PostgreSQL/PostGIS/PROJ/GEOS
  versions against the federation database's own, and records the diff as
  first-class observation evidence rather than a one-time discovery snapshot.
  **Decided:** GEOS is tracked alongside PostGIS/PROJ, not omitted — GEOS,
  not PostGIS version, governs the numeric behavior of the large majority of
  pushdown-eligible predicates that never call `ST_Transform`
  (`ST_Intersects`, `ST_DWithin`, `ST_Contains`, `ST_Buffer`, spatial joins),
  and PostGIS version is not a reliable proxy for it. H3 is dropped from this
  set entirely — a source database never holds MAPP-owned extensions like
  `h3`/`h3_postgis` (see **Architectural decision**), so there is no source-side
  H3 version to diff against. A mismatch is not itself a failure; the alias
  stays usable. But it must be visible, with plain guidance attached: version
  alignment is what makes geometry-transform pushdown eligible (see the PROJ
  decision above) and generally improves how much of a query can be safely
  pushed down instead of pulled and computed locally — an operator upgrading
  a source to match should see that as a concrete, explained performance
  lever, not something they'd only discover by noticing queries got faster.

Expose this as a `federation` mode of the existing `./bin/mapp verify`
command, alongside its current non-mutating role/privilege/drift audits:
running it should report, per alias, connectivity/schema/freshness state
*and* the full list of dependent relations whose output is now suspect — one
command that answers both "is anything stale" and "what does that break,"
rather than requiring an operator to cross-reference the alias registry
against the dependency guard by hand.

This status must be visible, not just runnable on demand. Anyone holding
`federation:observe` — not just `federation:register` /
`federation:provision` — should be able to see current topology: every
registered alias, its connectivity/freshness state, its extension-version
alignment against the federation database (with a plain-language note when
misalignment is limiting pushdown), and the integration, derived, and
materialized relations that depend on it. Surface this
identically in two places reading the same endpoint: a dashboard topology view
(extending the source/drift/provenance presentation already planned for
`config-ui/src/main.jsx` and `semantic.jsx`) and a `mapp-config-cli` status
command. Neither renders its own notion of "is everything alive" — both
display the same observation state the verifier already records, so the
dashboard and the CLI can never disagree about whether a source is reachable.

Reconcile this with the existing stated preference in [Semantic metadata
control plane](semantic-layer.md): "When a real second data location is
introduced, prefer a source-specific collector that owns only that source's
narrow read credential and emits the same closed generated-event contract." A
control-plane worker holding every source credential and a per-source collector
holding exactly one are different trust models.

**Decided:** one in-process worker for now, written to the per-source-collector
interface from day one — each source's credential access inside the worker is
already isolated behind that interface, so extracting it into N per-source
collector processes later is a deployment change, not a redesign. This
satisfies rather than contradicts the semantic page's stated preference;
[Semantic metadata control plane](semantic-layer.md) should get a one-line
cross-reference to this design instead of leaving the two pages looking like
an unresolved disagreement.

## Materialization

An ordinary federation or integration view stores only its definition. A
materialized output stores copied result rows in the federation database.
Materialization is therefore optional, disclosed, separately approved, and
subject to retention and data-residency policy.

A refresh must:

1. resolve ready semantic profiles for every source relation;
2. obtain current connectivity, schema, and configured version evidence;
3. validate the exact query with the universal computation guard;
4. enforce the separate 1 GiB planned-storage guard;
5. record the source observations used for the refresh;
6. build and validate a candidate atomically;
7. publish only a complete result from every required source;
8. leave the previous result intact on any failure;
9. report refresh time, observation time, source versions, and freshness state.

A source outage may leave an existing materialization readable, but it must not
be labelled current without evidence. Partial multi-source refresh is never an
acceptable replacement. A source update is not approval to refresh, and a
refresh is not approval to change the workspace.

Begin with manual confirmed refresh. Scheduling, alerting, retention
automation, CDC, and incremental refresh are later operational capabilities.

## Bundled test-data ETL as the reference federation

The Leeds and Census fixtures should become reference sources rather than an
exception that loads directly into the federation execution database.

### Target topology

```text
Leeds source database              Census source database
+----------------------+           +----------------------+
| ETL-owned relations  |           | ETL-owned relations  |
| native spatial index |           | native spatial index |
| publication record   |           | publication record   |
+----------+-----------+           +----------+-----------+
           | read-only FDW                     | read-only FDW
           +----------------+------------------+
                            v
                  federation database
              +----------------------------+
              | source_leeds.*             |
              | source_census.*            |
              | integration.*              |
              | derived_layers.*           |
              +----------------------------+
```

Separate Leeds and Census databases deliberately exercise database identity,
cross-source composition, independent failure, drift, and freshness. Separate
databases in one PostgreSQL cluster are appropriate for routine development,
but they are not sufficient evidence for external networking, TLS, or failure
isolation claims.

### ETL ownership

Test ETL continues to own:

- pinned upstream download and validation;
- staging and atomic source publication;
- stable source identifiers;
- row-count, hash, geometry, and SRID validation;
- native and required expression spatial indexes;
- `ANALYZE` after publication;
- a source dataset publication record;
- grants to a dedicated read-only source role.

It must not create federation foreign tables, integration views,
`derived_layers`, semantic records, workspace layers, MAPP dependency guards,
or source-side MAPP event triggers.

### Publication record

Each test source should expose a small, source-owned, closed publication
relation containing at least:

```text
dataset_id
release_id
schema_version
source_hash
published_at
row_counts
geometry_contract_version
```

The final publication record and stable source relations must become visible as
one atomic release. A failed ETL leaves the previous release and its version
record intact. The federation verifier reads at most the exact bounded version
record needed for the registered dataset; it does not repeatedly hash source
tables.

Two existing pieces already do most of this and should be extended rather than
replaced: the ETL run records `leeds._etl_runs` and `leeds._census_etl_runs`,
and the pinned manifest `instance/etl/census.json`, whose recorded geometry and
47 topic source hashes `scripts/verify.sh` already compares against the live
snapshot. Note that those underscore-prefixed run tables are deliberately
excluded from semantic discovery; a publication record intended to be *read* by
the federation verifier must not be given a name that the exclusion rule hides.

### ETL sequence

1. Download and validate pinned inputs without database publication.
2. Load staging relations in the relevant source database.
3. Validate hashes, row counts, identifiers, geometry, and SRIDs.
4. Create source-side indexes and update planner statistics.
5. Atomically publish stable source relations.
6. Publish the matching dataset version record in the same transaction or
   equivalent atomic publication boundary.
7. Commit the source release.
8. Let the federation verifier observe the release separately.
9. Require separate actions for materialized refresh and workspace changes.

The lifecycle invariant is:

```text
ETL publication
  != source registration
  != integration publication
  != materialized refresh
  != workspace apply
```

### Development and acceptance layouts

| Layout | Purpose |
| --- | --- |
| One cluster, source and federation databases | Fast everyday cross-database development |
| One cluster, separate Leeds, Census, and federation databases | Routine multi-source integration testing |
| Separate source and federation PostgreSQL services | Required external-network, credential, outage, and TLS acceptance |

Keep familiar ETL entry points where practical, but make their target explicit.
`etl`, `census-etl`, federation registration, federation verification, and
materialized refresh must not become one implicit privileged command.

### Reset semantics

The current broad bundled reset will need decomposition into ownership-aware
operations:

- reset bundled test sources;
- reset the federation database;
- reset platform workspace/control/semantic state only under its existing
  explicit contract.

Resetting a test source must not erase semantic, workspace, proposal, or audit
history. Resetting the federation must not remove source data. A recreated
physical database raises a source identity change; a test-only confirmed flow
may rebind the stable logical alias while recording the replacement.

**Decided:** resetting a source database must synchronously invalidate that
alias's cached observation — mark it stale/unknown at reset time — rather than
leaving it to the next cadence-driven check. The on-use freshness policy under
**Freshness and verification** deliberately serves cached state without a
live round trip when the observation is fresh by the clock; without an
explicit invalidation hook, a reset immediately after a fresh observation
would be masked for up to a full cadence window, serving results labelled
current from a source that was just wiped.

Decomposition is harder than it looks, because `reset-data` is not a volume
deletion with extra steps. It installs a durable owner-fenced PostgreSQL
maintenance gate under the derived-layer advisory lock, drains the semantic
outbox to a blocker-free preflight state, archives every current asset, and
only then removes the volume — with `recover-reset-data --confirm` as the
owner-pinned compensation path for an interrupted run. See
[Semantic metadata control plane](semantic-layer.md) and `bin/mapp:572`.

Federation adds cases that machinery has no answer for today, and each needs a
decision:

- resetting a **source** database while the federation database holds foreign
  tables pointing at it — the gate lives in the federation database, and
  nothing currently fences a source-side reset;
- whether the maintenance gate must be held across **all** databases or only
  the federation database;
- what a source reset does to semantic assets whose binding names that alias,
  given that archival is deliberately irreversible;
- how `bin/mapp:584` learns which of several named volumes to remove.

## Failure behavior

- A live view that requires an unavailable source fails closed.
- Layers independent of that source continue operating.
- A retained materialization may remain readable with visible age and source
  evidence.
- Verification failure never drops a relation or edits the workspace.
- Structural drift never triggers an automatic rebuild.
- A similarly named alternate source is never silently substituted.
- Cross-source creation and refresh are atomic.
- Background work is bounded and cannot monopolize remote connections.
- Planner estimates supplement but never replace PostgreSQL role resource
  ceilings.

## Backup and recovery implications

The federation database becomes MAPP-owned recoverable state and must be backed
up consistently with workspace, control, and semantic state. Each source
database remains under its source operator's independent backup policy.

Backups must preserve:

- source registrations and their connectivity/freshness observation
  history — both live in the federation database's control schema (see
  **Federation database layout**) — without exposing connection secrets;
- federation server/table definitions and integration/derived objects;
- derived definitions and semantic outbox state;
- semantic profiles, generated/curated proposals, audit, and workspace
  state — the pre-existing control/semantic stores, distinct from the
  federation-database observation history above;
- source and materialization provenance needed to report restored freshness.

Restoring a federation database does not prove that any remote source has been
restored to the same release. Startup must re-observe physical identity,
structure, and configured version signals before declaring live or cached
outputs current. FDW secrets and user mappings need an explicit secret-store
restore and rotation procedure.

Secret retention must be tied to backup retention, not treated separately: a
secret must not be hard-deleted, nor rotated with destruction of the prior
value, while any backup within the retention window could still reference its
`connectionRef`. The standard pattern is to version secrets and soft-delete —
retire a version only after the last backup that could restore to a state
referencing it has itself expired — rather than the reverse, where a restore
succeeds at the database level but leaves a foreign table with no valid
credential and no recorded reason why.

[Backup and restore](backup-restore.md) currently coordinates **two** stores
across one write-quiesced interval: PostgreSQL, which holds the derived
definitions and the semantic outbox, and SQLite, which holds delivered profiles
and event receipts. Its numbered restore order depends on that pairing.
Federation makes it **N + 2** — the federation database, the semantic SQLite
store, and each independently operated source — and no single write-quiesced
interval can span databases MAPP does not control.

The consequence must be stated rather than left implicit: a restored federation
is never provably consistent with its sources, only re-observed against them.
That is acceptable, but it means the restore order gains an explicit
re-observation step before any freshness claim, and the existing
production-acceptance restore hook — which today asserts semantic catalog
revision and derived-profile readiness — needs a federation-aware equivalent.

## Delivery waypoints

### Waypoint 0: identity and parsing hygiene

Prerequisite work with no federation dependency and independent value:

- consolidate the six ad-hoc relation-parsing sites into one `(alias, schema,
  relation)` contract, decided as alias-qualified from day one rather than
  extended later — see **Relation strings are currently un-parsed**;
- reconcile the two conflicting alias patterns onto the semantic allowlist
  grammar (`config-ui/semantic_sources.py:113`), tightening `DB_KEY` at
  `config-ui/workspace_schema.py:21` and `databaseKey` in
  `config-ui/schema/workspace.schema.json` to match;
- validate the zoom-keyed `tables` map against the catalog;
- validate and propagate `binding.alias` in the area-weighted H3 recipe;
- replace the literal environment-key enumerations at `bin/mapp:121` and
  `scripts/verify.sh:29` with a prefix rule;
- remove the hard-coded `DB_CONNECTIONS["MAPP"]` at `config-ui/app.py:604`.

None of this requires a decision about FDW, and all of it is required whichever
decision is taken.

### Waypoint 1: non-invasive external read mode

- Never run the bundled database upgrade against an external source.
- Produce a read-only compatibility and capability report.
- Support one external PostGIS source with a least-privilege runtime reader.
- Disable managed capabilities that require unavailable writes or extensions.
- Persist stable source identity, structural fingerprints, and observations
  outside the source.

Scope this honestly: most of it already exists. External mode already performs
no startup mutation, `./bin/mapp verify` already produces a non-mutating
capability and privilege audit, `GET /api/derived-layers/capabilities` already
reports staged H3 readiness, and semantic sync already persists structural
fingerprints outside the source. The genuinely new work is **observation
state** — connectivity, schema-compatibility, physical identity, and freshness
as first-class, separately reported facts with history.

### Waypoint 2: federation foundation

- Add the `federated` `MAPP_DATABASE_MODE` value and the operator-facing
  transition path from `bundled`/`external` into it.
- Introduce the dedicated MAPP-owned federation PostGIS database.
- Add the alias registry and secret-reference contract.
- Provision one explicit PostgreSQL FDW source.
- Make derived-layer identity source-aware (catalog and semantic identity
  already are).
- Route XYZ and configuration-service spatial reads through the federation **in
  the same step**, per the ordering constraint above.
- Generalize `scripts/verify.sh:1883` rather than deleting it.
- Verify on startup, before use, periodically, and on demand.

### Waypoint 3: integration products

- Add a separately owned integration schema and guarded lifecycle.
- Extend `derived_query_guard.py` with foreign-table provenance.
- Require semantic source declarations and provenance.
- Add dependency and drift-impact reporting.
- Preserve proposal, candidate-preview, apply, and reload separation.

### Waypoint 4: multi-source composition

- Register independent Leeds and Census sources.
- Support bounded, reviewed cross-source views.
- Enforce source-aware identifier and provenance rules.
- Add per-source failure reporting and representative plan/performance tests.

### Waypoint 5: federated materialization

- Add manual confirmed refresh first.
- Reuse universal computation and planned-storage guards.
- Record source observations and expose freshness.
- Publish atomically and retain the prior result on failure.
- Add retention and deletion controls.

### Waypoint 6: operational automation

- Add conservative schedules, retry/backoff, alerts, and capacity reporting.
- Extract the verifier only when scale or isolation requires it.
- Consider incremental refresh or CDC only for justified source contracts.

## Repository impact map for the implementation team

| Area | Files | Nature of the change |
| --- | --- | --- |
| Connection registry | `config-ui/app.py:100`, `:604` | Already multi-alias; remove the one hard-coded lookup |
| Catalog discovery | `config-ui/app.py:3690`, `:3739`, `:4324` | Add relkind `'f'`; validate the `tables` zoom map |
| Relation parsing | `config-ui/app.py:1298`, `:4419`, `:4613`, `control_api.py:3082`, `derived_layers.py:324`, `workspace_schema.py:27` | Consolidate to one contract before adding a dimension |
| Semantic source plane | `config-ui/semantic_sources.py` (whole file) | Already alias-aware; extend to relkind `'f'` and observation state |
| Semantic identity migration | `config-ui/semantic_sources.py:176`, `semantic-service/semantic_store.py:912` | Any identity change is a rebind migration |
| Derived layers | `config-ui/derived_layers.py:324`, `:762`, `:928`, `:1486`, `:1655`, `:2824`, `:3021`, `:3137`, `:3238` | Add the source dimension; re-establish declaration proof across FDW |
| Query guard | `config-ui/derived_query_guard.py:17` and the `pg_depend` walk | Add foreign-table provenance as a first-class class |
| Dependency guard | `config-ui/app.py:1327`, `:1382`, `:1406`, `:1429`, `docker/postgis/init/25-platform-layer-drop-guard.sql` | Already alias-keyed; make `"derived"` a real alias; stop writing into source databases and stop failing open |
| Workspace schema | `config-ui/schema/workspace.schema.json` | Reconcile the alias pattern; decide on alias-qualified relations |
| Wrapper lifecycle | `bin/mapp:121`, `:342`, `:380`, `:511`, `:572`, `:584`, `:657`, `:743` | Env guard, mode dispatch, per-alias URL validation, reset decomposition |
| Verifier | `scripts/verify.sh:29`, `:132`, `:205`, `:1033`, `:1883` | Per-alias audits; generalize the same-database invariant |
| Compose topology | `compose.yaml:19`, `:48`, `:101`, `:272`, `compose.bundled-db.yaml:5` | Additional database services, volumes, healthchecks; decide the network |
| Database images | `docker/postgis/init/*`, `upgrade-derived.sh:82`, `:116`, `:136`, `prepare-spatial-indexes.sh:69` | Split source-owned from federation-owned initialization; remove the unconditional role-password reset from any path that could reach a source |
| Environment | `.env.example`, `scripts/check_env.py`, `scripts/validate_database_url.py` | Per-alias variables; deliberate secret-generation naming |
| Dashboard | `config-ui/src/main.jsx:290`, `:294`, `:316`, `:341`, `semantic.jsx:525`, `:800` | Source, drift, provenance, and freshness presentation, including a topology view (aliases, reachability, dependent relations) |
| Documentation | every page listed under **Status and purpose** | Update the owning page in the same change as the behavior |

Do not reintroduce the standalone `config-cli` into this repository. Remote
operator workflows remain implemented and released from its separate
repository.

## Cross-repository impact: `mapp-config-cli`

This document is currently platform-only, which leaves half the delivery
undescribed. The CLI is an independently released client, and every federation
capability that an operator or agent must drive remotely becomes a
cross-repository contract change governed by [Repository
split](repository-split.md).

The platform is authoritative and the CLI must not reimplement validation, so
the platform side of the contract must land first:

| Contract surface | Current state | Federation requirement |
| --- | --- | --- |
| `apiVersion` / `contractVersion` | `1.4` | A minor bump for additive federation commands; a major bump if relation identity changes shape |
| `rulesVersion` | `1.6` | Bumps if workspace relation or `dbs` validation changes |
| `contracts/api-compatibility-v1.4.json` | Declares consumers and pagination endpoints | A new versioned artifact; alias and observation collections are growing collections and need pagination entries |
| `GET /api/contract` | Runtime authority for command names | Must advertise new federation command names |
| `GET /api/capabilities` | Runtime authority for action IDs, risk classes, conditional scopes | Must advertise federation actions, their risk class, and their exact scope combinations |
| Scope model | `derive`, `semantic:*`, etc., explicitly non-hierarchical | New `federation:register` / `federation:provision` / `federation:observe` scopes, plus a separately-granted **reclaim** action for re-registering a retired alias's name; reachable from the dashboard session or from a CLI credential holding them, peer to each other, not reachable from any other existing scope. `federation:provision` also gates Discover and the verify-not-read endpoint, not just Approve exposure |
| Pagination | Contract `1`, 100-item pages, opaque cursors | Alias lists, relation lists, and observation history all need cursors; note that the semantic cursor scope already binds a digest of alias configuration at `config-ui/semantic_sources.py:339`, so adding aliases invalidates cursors by design |
| Topology view | No equivalent today | New read-only `federation status` command gated by `federation:observe`, backed by the same observation endpoint the dashboard's topology view renders — must never diverge from the dashboard's answer to "is everything alive" |

The CLI must fail closed against an older or newer server, which the existing
contract already requires: a similar action ID or matching route does not grant
a missing command. Federation must not weaken that. The compatibility gate
enumerated in [Repository split](repository-split.md) needs federation rows —
at minimum alias discovery, observation reads, a denied registration attempt
from a credential lacking `federation:register`, and an allowed registration
and provisioning attempt from CLI credentials that hold `federation:register`
and `federation:provision` respectively.

Two further cross-repository decisions belong in the CLI repository's own
handoff and should be raised there rather than assumed here: whether the CLI
ever handles a source connection secret at all (it should not), and how it
presents per-source degradation so an agent cannot mistake a retained
observation for a live one.

## Required acceptance evidence

The architecture is established only when a representative test demonstrates:

1. Leeds and Census publish into independent source database identities.
2. Source databases remain unchanged by federation operations apart from
   separately approved read-role provisioning.
3. Approved relations appear under distinct alias identities in the federation
   database.
4. A guarded integration view combines data from both sources.
5. An XYZ workspace layer renders and interacts with that integration view.
6. Semantic annotations survive one source becoming unavailable.
7. The affected path is reported as degraded rather than forgotten.
8. A structural or physical-identity change is detected and not silently
   rebound.
9. A failed source ETL leaves its prior atomic release available.
10. A new source release makes an older materialization visibly stale or of
    unknown freshness according to its declared strategy.
11. A failed or partial multi-source refresh leaves the prior materialization
    intact.
12. Independent layers continue operating during an unrelated source outage.
13. No credential or database URL appears in workspace, semantic, proposal,
    audit, log, screenshot, or artifact output.
14. Existing universal SQL-shape, bounded-H3, recursive-plan, PostgreSQL-plan,
    1 GiB materialization, role-resource, background-admission, semantic,
    proposal, preview, reload, and visual-evidence guards still pass.
15. External acceptance uses genuinely separate source and federation services;
    a second database in the bundled container alone is not claimed as that
    evidence.
16. Registering an alias beyond the platform-wide ceiling (50 by default) is
    rejected, and the ceiling only changes via redeploy with a new
    `MAPP_FEDERATION_MAX_ALIASES` value, never through a runtime call.
17. A credential holding only `federation:register` cannot itself complete
    "Approve exposure" — provisioning fails without a separately granted
    `federation:provision` credential (or the dashboard session).
18. A derived-layer query that references a `source_<alias>` schema directly
    is rejected by the query guard; only a reviewed integration/derived
    relation is reachable from a submitted query.
19. A workspace layer definition that names a foreign table directly is
    rejected at validation, before create or preview.
20. The "verify, not read" credential endpoint returns only a match/mismatch
    result and a last-verified timestamp for both a correct and an incorrect
    resend — never the stored secret value, in the response or in any log.
21. The dashboard topology view and the CLI `federation status` command report
    identical alias, connectivity, and dependency state for the same
    underlying observation.
22. A deployment switched into `federated` mode with zero registered aliases
    behaves identically to the same deployment before the switch.
23. A geometry transform is pushed down when the alias's observed PROJ
    version matches the federation database's, and executes locally when it
    doesn't — confirmed by comparing `EXPLAIN` output, not just result
    correctness.
24. A credential holding only `federation:register` (no `federation:provision`)
    is rejected when it attempts Discover — registering an alias does not by
    itself grant the ability to connect to it.
25. The verify-not-read endpoint rate-limits and alerts distinctly after
    repeated mismatches against the same alias, without locking out the
    legitimate credential holder.
26. A registration that never reaches Approve exposure auto-expires on its
    TTL and its slot becomes available against the alias cap again.
27. Retiring an alias and immediately attempting to re-register the identical
    name fails closed; the same name only succeeds after an explicit,
    separately-scoped reclaim action.
28. Resetting a source database immediately invalidates that alias's cached
    observation — a request served right after reset does not report the
    prior cached freshness label as current.
29. A GEOS version mismatch between an alias and the federation database is
    recorded as observation evidence distinct from PostGIS/PROJ, and disables
    pushdown eligibility for non-transform spatial predicates accordingly.

This list is not a new evidence system. It must be expressed through the
machinery the repository already has, or it will not be run:

- **`./bin/mapp verify`** owns items 2, 3, 13, and 14 — it already performs
  non-mutating role, privilege, index, guard-object, and drift audits. Each
  becomes a per-alias audit.
- **`./bin/mapp production-acceptance`** owns the rehearsal items, now
  including 22, 23, and 28. Its `pending` status is the honest home for
  anything that cannot be observed without real separate infrastructure, and
  items 15 and 23 in particular must stay `pending` rather than being
  satisfied by a second database in one container or a mocked PROJ version. A
  source-outage rehearsal hook joins the existing backup, restore, upgrade,
  and rollback hooks; item 28 extends that same hook to assert observation
  invalidation at reset time.
- **Browser candidate evidence** owns items 5, 12, and the dashboard half of
  21.
- **The platform's existing pytest suite (`config-ui/tests/`)** owns items
  16–20 and 24–27, 29 — each is a direct API-behavior assertion with an
  existing sibling to follow: item 18 alongside `test_derived_query_guard.py`,
  item 19 alongside `test_workspace_schema.py` / `test_workspace_json_schema.py`,
  item 24 alongside whichever test module covers Discover's scope check, and
  the rest as new tests following the pattern already set by
  `test_semantic_sources.py` and `test_control_api.py`.
- **The `mapp-config-cli` compatibility gate** ([Repository
  split](repository-split.md)) owns the CLI half of 17 and 21 — a denied
  registration from a credential lacking `federation:register`, and a CLI
  `federation status` result that must never diverge from the dashboard's.
- **[Validation log](validation-log.md)** owns the record. Follow its existing
  discipline exactly: `keep` means the gate passed, `blocked` means it could
  not complete for an environmental reason and is not a pass. The log already
  contains a `blocked` row for live external PostGIS acceptance because no
  separate database was available, and an earlier attempt against the bundled
  service was explicitly rejected as invalid evidence. Federation acceptance
  will produce more rows of exactly that kind; do not resolve them by
  redefining the gate.

Run validation in proportion to each waypoint and report exactly what ran. Do
not claim external routing, backup, recovery, materialization, outage handling,
or source non-mutation without its relevant integration evidence.

## Decisions to resolve before implementation

The next design pass must explicitly resolve, rather than silently assume:

1. **Decided:** identity stays `alias`. No new `sourceId` is introduced — per
   this page's own **Naming** section, reserving `sourceId` requires a
   deliberate reason stated in the same change, and none has been found. This
   also avoids the alternative's real cost: renaming `alias` inside the
   semantic identity string is a migration that rebinds every existing
   semantic asset UUID, not a refactor.
2. **Decided:** add a third mode value (`federated`). `bundled` and `external`
   keep their current meaning unchanged and are entirely unaffected — no
   existing deployment needs to become expressible as a degenerate
   federation, because federation-specific state and behavior are reachable
   only in `federated` mode. Within `federated` mode itself, zero registered
   aliases must still be a valid, safe state. See **Deployment topology and
   database mode**.
3. **Decided:** the federation PostgreSQL database. Alongside integration and
   derived objects, under one backup/restore boundary, consistent with "only
   the federation database receives MAPP-owned extensions/state" in
   **Architectural decision**.
4. **Decided:** FDW objects and user mappings may be provisioned either from
   the administrator dashboard session or from a `mapp-config-cli` credential
   holding `federation:provision` (registration itself gated separately by
   `federation:register`); the two authorities are peers, not a hierarchy —
   see **API scopes, not only database roles**. `federation:provision` also
   gates Discover, not `federation:register` alone, and gates the
   verify-not-read credential-check endpoint. A third scope, `federation:observe`,
   covers passive topology/freshness/provenance reads only. A platform-wide
   ceiling of 50 registered aliases (env-configurable) bounds the aggregate
   risk of self-service registration: it is enforced under a serializing
   check, `pending` registrations auto-expire on a TTL, and `retired` aliases
   never count toward it. Re-registering a retired alias's name requires an
   explicit, separately-scoped **reclaim** action (see **Source lifecycle >
   8. Drift and retirement**). Agentic/unattended CLI use of these scopes is
   accepted. Still open: the exact grant/revoke flow, the TTL length, and
   whether any of these scopes should ever be scoped to a subset of aliases
   rather than granted blanket.
5. **Decided (referencing and backup halves):** secrets are referenced via the
   write-only submission endpoint and the separate verify-not-read endpoint
   (see **Credential, egress, and configuration boundary**), and secret
   retention is tied to backup retention via versioning/soft-delete, never
   hard-delete (see **Backup and recovery implications**). Still open:
   rotation mechanics, the exact restore procedure, and whether registered
   hosts require a reviewed versioned `instance` input comparable to the
   browser egress allowlist.
6. **Decided:** the layer dependency guard moves entirely into the federation
   database. No configuration retains a write path into a source database;
   the fail-open behavior at `config-ui/app.py:1429` is removed outright, not
   made conditional — absence or failure of the guard sync is always
   reported, never swallowed.
7. How the derived-layer declaration proof is re-established when `pg_depend`
   resolves a local foreign table rather than the declared remote relation.
8. **Decided (policy half):** `FORBIDDEN_RELATION_SCHEMAS` inverts to an
   allowlist — only integration/derived and named system schemas are readable
   by a submitted derived query; every `source_<alias>` schema is denied by
   default. Still open: how `derived_query_guard.py` classifies foreign
   tables as a first-class provenance class (resolving
   `pg_foreign_table`/`pg_foreign_server`, requiring the approved
   `postgres_fdw` handler) — that mechanics work still needs a real foreign
   table to design against.
9. Whether planner-estimate admission requires `use_remote_estimate` per alias,
   and what bounds remote work when local timeouts do not apply.
10. The exact physical database identity evidence available on managed and
    restricted PostgreSQL services.
11. **Decided:** never — integration/derived relations only, from day one.
    See **Federation database layout > Foreign source schemas**.
12. **Decided:** the reconciled alias pattern is the existing semantic
    allowlist grammar, `[A-Za-z][A-Za-z0-9_-]{0,62}` — chosen because it
    already respects the PostgreSQL 63-byte identifier limit that `source_`
    plus the alias must fit inside. `config-ui/workspace_schema.py:21`
    (`DB_KEY`) and `databaseKey` in `config-ui/schema/workspace.schema.json`
    must be tightened to match; any existing alias outside the new pattern
    needs an explicit migration note in the change that lands this, not a
    silent validation break. The generated schema name is immutable once
    created — renaming an alias means retiring it and registering a new one,
    never an in-place `ALTER SCHEMA`, consistent with how a physical-identity
    change is already handled elsewhere on this page. Ownership belongs
    solely to the FDW provisioner (no runtime or derived role may create,
    alter, or drop it, per **Roles and trust boundaries**), and registration
    fails closed if `source_<alias>` already exists as a schema for any
    reason, rather than auto-disambiguating a collision. The one collision
    with a defined resolution is a retired alias's own name: since retirement
    archives rather than drops the physical schema, re-registering the same
    name requires the explicit **reclaim** action described under **Source
    lifecycle > 8. Drift and retirement**, not an undocumented manual
    intervention.
13. How source RLS, security-barrier views, and FDW pushdown are verified,
    given that per-user RLS does not survive a shared user mapping.
14. **Decided:** geometry transforms may be pushed down only when the alias's
    observed PROJ version exactly matches the federation database's;
    otherwise they execute locally. Enforced via `postgres_fdw`'s per-server
    `extensions` option, set at Approve exposure and updated only by an
    explicit `federation:provision`-scoped reprovisioning triggered by a
    drift observation — not a live per-query check. Tracked versions include
    GEOS alongside PostGIS/PROJ; H3 is not tracked on sources. See **The
    derived-layer query guard and foreign tables** and **Freshness and
    verification**.
15. The closed contract for publication/version relations and timestamp checks.
16. How semantic field identity behaves across explicit source rebinds.
17. How restored materializations are labelled until all contributing sources
    have been re-observed.
18. How `reset-data`'s owner-fenced maintenance gate behaves across a source
    database it does not own.
19. **Decided:** one in-process worker, built to the per-source-collector
    interface from day one, so later extraction into N collectors is a
    deployment change rather than a redesign. See **Freshness and
    verification**; [Semantic metadata control plane](semantic-layer.md)
    needs a one-line cross-reference added so the two pages don't read as
    disagreeing.
20. Which of the source-owned mutation sites listed above are removed, moved to
    the federation database, or made explicitly opt-in per alias.
21. The exact migration and rollback path from the current bundled database
    without claiming that moving definitions also moves or verifies data.

## Recommended first implementation task

Start with a design-and-test slice, not a Compose rewrite. The most valuable
first slice is the one already half-present in the code.

Today `_dependencies()` at `config-ui/derived_layers.py:1667` admits relkind
`'f'`, while catalog discovery and semantic sync do not. A foreign table can
therefore be detected as a dependency but can never obtain the ready semantic
profile that create requires. The system is fail-closed by accident rather than
by decision. Turning that into a deliberate, tested contract exercises source
identity, semantic registration, guard provenance, and the declaration proof
without provisioning a single FDW object:

1. Review and confirm the mutation classification under **Classifying current
   database mutations**, and decide the disposition of each source-owned site
   listed there — in particular `sync_layer_dependency_guard()` and the
   source-schema index and `ANALYZE` work.
2. Complete the Waypoint 0 parsing and identity hygiene above.
3. **Decided:** admit relkind `'f'`. Make catalog discovery
   (`config-ui/app.py:3714`) and semantic sync
   (`config-ui/semantic_sources.py:53`) admit it, matching what
   `_dependencies()` at `config-ui/derived_layers.py:1667` already does —
   closing the accidental fail-closed gap deliberately, before any real
   foreign table exists to test against. This does not by itself resolve the
   query guard's FDW-provenance work (decision 8 above), which still needs an
   actual `pg_foreign_table`/`pg_foreign_server` to design against.
4. Define the alias, relation, observation, physical identity, and freshness
   API schemas with closed validation contracts.
5. Specify read-only external capability detection and failure responses.
6. Add contract tests proving that capability inspection and observation
   perform no writes against a source.
7. Design the Leeds/Census publication record and atomic ETL boundary.
8. Present the focused architecture/API diff, and the matching CLI contract
   diff, for review before adding FDW or restructuring deployment topology.

That slice establishes the identities and invariants on which every later
federation feature depends.
