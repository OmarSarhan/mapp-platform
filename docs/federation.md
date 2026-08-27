# Federated PostgreSQL sources

Federation attaches a **separate PostgreSQL database** to MAPP over
`postgres_fdw` and exposes an explicitly approved list of its relations as
foreign tables. Derived layers, workspace layers and semantic profiles can then
read that source as if it were local, without MAPP holding a copy of the data.

What it deliberately does not do: it does not replicate, it does not write to
the source, and it does not make the source's schema authoritative. A source is
approved once, verified continuously, and can be withdrawn.

## Before you start

Federation needs a **local database**, which means
`MAPP_DATABASE_MODE=bundled` or `federated`. The configuration service
disables the alias registry entirely under `external`, because MAPP does not
administer that server and must not create foreign servers on it.

`federated` is currently **identical to bundled in every respect**. It exists
so a deployment can name the intent to attach federated sources before doing
so; it will grow its own behaviour only when it needs different behaviour.

Switching mode is one line, and a `federated` deployment with **no registered
aliases** is a fully supported steady state, not a half-migrated one:

```bash
sed -i 's/^MAPP_DATABASE_MODE=bundled$/MAPP_DATABASE_MODE=federated/' .env
./bin/mapp up
./bin/mapp verify
```

Both modes resolve the same Compose files and the same service list, so
`docker compose config` differs only in the mode value itself, and `verify`
produces the same output. Note that zero aliases does not mean zero
configuration: any local database still requires `FEDERATION_DATABASE_URL`
and `FEDERATION_DB_USER`, and `verify` exits 2 without them. The alias audit
itself is a loop over provisioned aliases, so an empty registry satisfies it
trivially. This equivalence is what makes the mode safe to
adopt before any source exists — you are not committing to a migration by
naming it. `./bin/mapp federation-test` runs under either mode.

The equivalence is pinned by a test rather than left to habit
(`test_every_bundled_mode_comparison_admits_federated` in
`scripts/tests/test_database_access_contract.py`): any comparison against the
literal `bundled` mode must name `federated` alongside it. Widening the shell
and Python guards while missing one inside an embedded script is exactly how
the runtime-reader probes were once skipped under `federated`, leaving a
reader that could not serve the sample tables passing `verify`.

The lifecycle is split deliberately, along the line where a **secret** enters
the system. Registration introduces a credential, so it belongs in the
environment and is driven from the CLI; it is not offered in the dashboard,
and a form there would either need the environment variable anyway or accept
the credential over HTTP, which would be worse. Everything after that is state
the dashboard shows and an exposure switch it can flip:

| Step | Where |
| --- | --- |
| Register | CLI only, against a `FEDERATION_DBS_<REF>` already in the environment |
| Observe, provision, retire | CLI, or the dashboard's **Federated sources** panel |

Mint the CLI credential from the dashboard's scoped-token form. The
**Federation operator** preset grants exactly `federation:observe`,
`federation:register`, and `federation:provision`; **Federation observer**
grants read-only. Prefer either over a `full` token: `federation:provision` is
the only scope that can serve a third-party database, and a full token carries
every other authority for thirty days as well.

Every step below is shown as an HTTP call, and each has an equivalent
`config-cli` subcommand that carries the profile, token, and contract
handshake for you:

```bash
config-cli federation list
config-cli federation show leeds_ext
config-cli federation register leeds_ext --connection-ref LEEDS_EXT \
  --relation leeds.smoke_control_orders \
  --data-handling 'Public open data, OGL v3.' --acknowledge-data-handling
config-cli federation observe leeds_ext
config-cli federation provision leeds_ext --expected-observation-id 88 --confirm
config-cli federation retire leeds_ext --confirm
```

The `federation:*` scopes are elevated and are not part of the default device
credential, so request them explicitly:

```bash
config-cli auth device --scope federation:observe --scope federation:register \
  --scope federation:provision
```

Each source needs a credential in the environment, named by convention:

```bash
# .env — one per source. The suffix after FEDERATION_DBS_ is the connectionRef.
FEDERATION_DBS_LEEDS_EXT=postgresql://reader:secret@source-db:5432/sourcedb?sslmode=require&gssencmode=disable
```

These are kept out of `DB_CONNECTIONS` on purpose: ordinary catalogue, layer
and semantic discovery cannot reach a federation source, and only explicitly
scoped Observe and Provision calls resolve the credential.

The connection string must set `host`, `dbname`, `user`, `password`,
`gssencmode=disable`, and an `sslmode` at least as strong as the alias's
declared `tlsPolicy`. Anything else is rejected before a connection is opened.

## Getting a token

The four scopes are `federation:register`, `federation:observe`,
`federation:provision`, and they are **not hierarchical** — holding one grants
nothing else. Mint a token from an administrator session:

```bash
curl -sX POST https://config.example/api/admin/tokens \
  -H 'Content-Type: application/json' \
  -b "$ADMIN_SESSION_COOKIE" \
  -d '{
        "name": "federation-operator",
        "expires": "2026-12-31T00:00:00+00:00",
        "scopes": ["federation:register", "federation:observe", "federation:provision"]
      }'
```

The token is shown once. Note that **Observe requires
`federation:provision`, not `federation:observe`** — it opens an outbound
connection to a third party, which is a heavier act than reading the registry.
`federation:observe` covers only the read-only list and show endpoints.

## The lifecycle

Four steps, each an explicit decision: **register**, **observe**, **provision**,
and later **retire**.

### 1. Register

Records intent. No connection is made and nothing is exposed.

```bash
curl -sX POST https://config.example/api/federation/aliases \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "alias": "leeds_ext",
        "displayName": "Leeds external",
        "kind": "postgresql",
        "connectionRef": "LEEDS_EXT",
        "tlsPolicy": "require",
        "allowedRelations": ["leeds.smoke_control_orders"],
        "dataHandlingClassification": "Public open data, OGL v3.",
        "dataHandlingAcknowledged": true
      }'
```

| Field | Notes |
| --- | --- |
| `alias` | `^[A-Za-z][A-Za-z0-9_]{0,55}$`. Becomes the schema name `source_<alias>`. |
| `kind` | `postgresql` only. |
| `connectionRef` | The suffix of a `FEDERATION_DBS_<REF>` variable. |
| `tlsPolicy` | `require`, `verify-ca`, or `verify-full`. Enforced against the actual connection string, not taken on trust. |
| `allowedRelations` | `schema.relation`, up to 100. This is the whole exposure surface — nothing outside it is ever imported. |
| `dataHandlingAcknowledged` | Must be literally `true`. Licensing, attribution and personal-data implications are the registering principal's to acknowledge. |
| `freshnessStrategy` | Optional; only `manual` is implemented. |

The registry holds at most **100 aliases**, retired ones included.

### 2. Observe

Connects, probes, and records what it found. Still exposes nothing.

```bash
curl -sX POST https://config.example/api/federation/aliases/leeds_ext/observe \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

The response carries a `lastObservationId` and a `lastObservation` describing
connectivity, schema state, a schema fingerprint, the physical database
identity, and detected row-level security. Read it before approving: this is
the evidence provisioning will be checked against.

### 3. Provision

The only step that exposes data. It re-verifies everything live, then creates
the physical objects and grants read access.

```bash
curl -sX POST https://config.example/api/federation/aliases/leeds_ext/provision \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"expectedObservationId": 88}'
```

`expectedObservationId` is required and must match the observation you read.
If anything moved in between, provisioning refuses rather than approving
something you did not look at.

Three conditions need explicit acknowledgement, each its own opt-in boolean
alongside `expectedObservationId`. These are the wire names; the snake_case
spellings that appear in `FederationAliasStore.provision()` are internal
keyword arguments and are rejected by the route:

| Property | When you need it |
| --- | --- |
| `rowLevelSecurityAcknowledged` | The source has RLS, so what MAPP sees depends on the reading role. |
| `schemaChangeAcknowledged` | The schema fingerprint moved since the accepted one. |
| `physicalRebindAcknowledged` | The source is a **different physical database** than the one previously approved. |

That last one is the important guard: a restored-from-backup or swapped source
keeps every name and column identical while being a different database.
Provisioning refuses with `federation.physical_rebind_not_acknowledged` unless
you say so deliberately.

A successful provision creates, in the bundled database:

- a foreign server `<alias>_srv`,
- user mappings holding the remote credential,
- a schema `source_<alias>`,
- foreign tables for the allowed relations only,
- `USAGE` and `SELECT` for the derived-layer owner and the map reader.

### 4. Retire

Withdraws a source. **Archives rather than drops** — the schema, server and
foreign tables are renamed out of the way so the audit trail stays physically
inspectable.

```bash
curl -sX POST https://config.example/api/federation/aliases/leeds_ext/retire \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

It refuses while anything still reads the source: a managed derived layer, a
workspace layer pointing at `source_<alias>.<relation>`, or an unreachable
semantic service (because it cannot then mark the profiles). Deal with the
dependants first.

Retirement is **terminal**. The alias keeps its row and its full observation
history, disappears from the normal alias list, and the name cannot be
re-registered. It still occupies one of the 100 slots.

The one thing retirement does delete is the **user mappings**. A mapping is not
audit evidence — it is a live credential held in the catalogue in plain text —
and a decommissioned source has no business keeping working credentials.

## Continuous verification

Once a source is provisioned, a background pass re-observes it **every 15
minutes**, measured from the start of one pass to the start of the next. The
first pass runs at startup, and the service waits up to 60 seconds for it
before accepting requests.

The pass acts on what it finds:

- **Source healthy and evidence current** → access stays granted.
- **Source unreachable, or evidence no longer matches** → the alias becomes
  `unavailable` and both consumer roles lose access. **This is automatic and
  reverses itself**: the next pass that succeeds grants access straight back.
- **`FEDERATION_DBS_<REF>` removed, or the connection string became unusable**
  → access is withdrawn until the configuration is repaired.

Aliases are taken least-recently-verified first, and a pass stops starting new
work after 10 minutes so one slow source cannot starve the rest. Anything it
defers leads the next pass.

An alias approved before the current evidence columns existed is skipped
rather than revoked, because nothing a timer can do would restore it — reprovision
it, and `acceptedEvidenceComplete` on the alias record tells you which need it.

## Semantic profiles follow the source

A semantic profile generated from a federated relation carries `sourceState`.
It is `null` while the source is usable and `"unavailable"` once verification
or retirement says otherwise. The profile, its ID and its curated meaning are
**retained** throughout, so a source that comes back reclaims exactly its own
annotations rather than needing them recreated.

While a profile is flagged, derived planning will not accept it — neither
`require_semantic_derived_sources` nor the area-weighted H3 recipe path — so
you get a refusal naming the source instead of a permission error on a schema
that has been renamed away.

`sourceState` is separate from `status` on purpose. `archived` is a decision
you made; `sourceState` is an observation that reverses itself. An archived
profile whose source also vanished carries both.

## Testing it

```bash
./bin/mapp federation-test
```

This drives the whole lifecycle against a genuinely separate `source-db`
container: register, observe, provision, a cross-schema H3 aggregation checked
against the same computation run locally, a replaced-database refusal, semantic
degradation and recovery, retirement, archival, and the privilege audit. It is
repeatable and cleans up after itself.

It refuses to run when `MAPP_ENVIRONMENT=production` or `MAPP_DATABASE_MODE`
is neither `bundled` nor `federated`, aborts rather than destroying a
pre-existing `e2e_probe` alias or probe relation, and refuses if recreating
`config-ui` would strip connection references the running container has.

The rig deliberately uses plain `postgis/postgis`, not MAPP's own H3 image — a
genuinely third-party source would not have MAPP's extensions installed, and
pretending otherwise would hide real incompatibilities.

## The two-source demo

A worked example of the arrangement this document describes: census data in one
database, the operational layers in another, both reached over `postgres_fdw`,
and MAPP's own database holding only derived output, the registry and the
`source_<alias>` foreign tables.

```bash
./bin/mapp init          # writes .env, including the demo keys and secrets
./bin/mapp all           # bundled stack plus sample data

docker compose --project-directory . --env-file .env \
  --file compose.yaml --file compose.bundled-db.yaml \
  --file compose.federated-demo.yaml up -d census-db ops-db config-ui

./docker/demo-sources/seed.sh     # copy sample data into the two sources
./docker/demo-sources/layers.sh   # register, observe, profile, provision, publish
./bin/mapp verify
```

`seed.sh` copies rather than moves, so the bundled schema stays intact and the
whole arrangement reverses by dropping the `census_postgres_data` and
`ops_postgres_data` volumes.

`layers.sh` is idempotent: every step tolerates its own prior success, so a
partial run can be repeated rather than unpicked. It mints a short-lived
scoped token, does the work, and revokes it on exit even if a step fails.

The derived layers it builds read `source_census.*` and `source_ops.*` rather
than any local schema, and reproduce the equivalent local computation exactly
— 18,251 H3 cells and 1,951,531 people for the census layer, 3,453 cells and
297,881,600 m² for smoke control. That equality is the point: the same answer,
from two separate database servers.

If the federation-test rig is also registered, name both overlays when
recreating `config-ui`, since each forwards only its own
`FEDERATION_DBS_<REF>`.

## Semantics are a second decision

Registering an alias exposes its relations. It does **not** permit their column
metadata to be profiled. That is `SEMANTIC_SOURCE_ALLOWLIST`, and it is
deliberately separate: serving a relation on a map and letting its column names
into a catalog that `semantic:generate` may send to an external model are
different questions, and a source can reasonably be fine for the first and not
the second.

The practical consequence is that standing up a federated source is four steps,
not one:

```bash
config-cli federation register census --connection-ref CENSUS ...   # 1. expose
# 2. permit profiling: add MAPP:source_census.* to SEMANTIC_SOURCE_ALLOWLIST
#    in .env, then recreate config-ui so it re-reads the variable
config-cli semantic source sync --alias MAPP \
  --schema source_census --relation census_2021_england_oa           # 3. profile
config-cli federation provision census --expected-observation-id N --confirm
```

Step 2 is easy to miss because skipping it fails late and indirectly: creating
a derived layer refuses, since every declared source must have a ready semantic
profile. Two things now make it visible. The refusal names the exact selector
to add:

```
The requested relation is not allowed as a semantic source.
MAPP:source_census.census_2021_england_oa is not in SEMANTIC_SOURCE_ALLOWLIST.
Add MAPP:source_census.* ...
```

and the dashboard's **Federated sources** panel reports coverage per alias —
"1 of 2 exposed relation(s) profiled" — with the selector needed, so the state
is legible before anything refuses.

Exclusions still apply on top: `SEMANTIC_SOURCE_EXCLUSIONS` is subtracted from
the allowlist, and the refusal says so when an exclusion is what is blocking.

## Predicate pushdown

Provisioning declares PostGIS shippable to `postgres_fdw` — `ALTER SERVER ...
OPTIONS (extensions 'postgis')` — but only when the two databases report the
same `postgis`, `postgisExtversion`, `proj` and `geos` versions. That gate
exists because `postgres_fdw` assumes a shippable extension's operators mean
the same thing on both sides; two PostGIS builds that differ could evaluate
the same predicate differently, and the wrong rows would come back with no
error.

The consequence is worth understanding, because it is invisible until you look
at a plan. With the option set, a spatial filter executes on the source:

```
Foreign Scan
  Remote SQL: SELECT count(*) FROM leeds.smoke_control_orders
              WHERE ((geom_3857 OPERATOR(public.&&) '...'))
```

Without it, the same query pulls every row's geometry across the wire and
filters locally:

```
Foreign Scan
  Filter: (geom_3857 && '...')
  Remote SQL: SELECT geom_3857 FROM leeds.smoke_control_orders
```

Both return the same answer. On a small allowlisted relation the difference is
unnoticeable; on a large one it is the difference between a remote index scan
and a full transfer.

If a source stops matching versions, provisioning removes the option again
rather than leaving a stale claim in place, and the alias keeps working at the
slower plan. Check with:

```bash
config-cli federation show <alias>
```

and, in the local database, `SELECT srvoptions FROM pg_foreign_server`.

A version drift on **MAPP's own** database has the same effect. A volume
initialised against an older image keeps its recorded extension version after
the image's PostGIS moves, so `extversion` falls behind
`PostGIS_Lib_Version()` and every source stops matching. `./bin/mapp
upgrade-derived` realigns it; `./bin/mapp verify` compares the two.

## Error codes

| Code | Meaning |
| --- | --- |
| `federation.not_configured` | Not a local-database mode (`bundled` or `federated`), or no federation database. Permanent for that deployment. |
| `federation.invalid_request` | Malformed or unknown properties in the body. |
| `federation.alias_limit_reached` | Registering would exceed the 100-alias ceiling, retired ones included. |
| `federation.alias_limit_exceeded` | The registry already holds more aliases than the ceiling allows — only reachable if rows were written directly to the database. |
| `federation.connection_ref_not_found` | No `FEDERATION_DBS_<REF>` for that reference. |
| `federation.tls_policy_not_met` | The connection string is weaker than the declared `tlsPolicy`. |
| `federation.observation_not_current` | `expectedObservationId` does not match the latest observation. |
| `federation.invalid_observation_id` | `expectedObservationId` missing or not a positive integer. |
| `federation.schema_change_not_acknowledged` | Fingerprint moved; needs `schemaChangeAcknowledged`. |
| `federation.physical_rebind_not_acknowledged` | Different physical database; needs `physicalRebindAcknowledged`. |
| `federation.row_level_security_not_acknowledged` | Source has RLS; needs `rowLevelSecurityAcknowledged`. |
| `federation.import_incomplete` | Foreign-table import did not produce the expected relations. |
| `federation.local_state_invalid` | Local objects exist but are owned by another role, or otherwise not what provisioning expects. |
| `federation.alias_in_use` | A derived or workspace layer still reads the source. |
| `federation.derived_layers_busy` | A derived-layer operation holds admission; retry. |
| `federation.verification_in_progress` | Another observation holds the alias lock; retry. |
| `federation.semantic_unavailable` | The semantic service could not be updated, so retirement would leave stale profiles. |
| `federation.workspace_unreadable` | The workspace could not be read, so dependent layers cannot be ruled out. |
| `federation.alias_retired` | The alias is retired; provisioning it is refused. |
| `federation.already_retired` | Retiring something already retired. |
| `federation.alias_not_found` | No alias by that name. Retired aliases are still found by exact name. |
| `federation.registry_unavailable` | The local registry database is unreachable. |

## Traps worth knowing

- **Observe needs `federation:provision`.** `federation:observe` is read-only.
- **Source collations must be `C`** for imported relations, or the portability
  check refuses. Collation differences change sort order and comparison
  results across the FDW boundary.
- **The test rig's certificate is self-signed**, so only `sslmode=require`
  works against it. `verify-ca` and `verify-full` need a real chain.
- **`freshnessStrategy` accepts only `manual`.** The other strategies are
  described in the architecture document but not implemented.
- **Retired aliases still occupy a slot** and their names cannot be reused.
- **PostGIS, PROJ and GEOS versions must match** between the federation
  database and the source, because a pushed-down spatial expression is
  evaluated by whichever side the planner chooses.

---

## Deeper detail

The rest is mechanism. You do not need it to operate federation.

### Evidence, and why it is durable

Provisioning records three accepted values on the alias:
`accepted_schema_fingerprint`, `accepted_physical_identity` and
`accepted_connection_identity`. Every later observation compares what it finds
against those, and access is granted only when all three match and are
non-null.

The distinction that matters: an **observation** is what the world looks like
now, routinely overwritten by background work; an **accepted** value is a
durable record of what a human approved, moved only by an explicit Provision.
Comparing live state against another live reading would let a source drift into
approval one small step at a time.

### Naming and the 63-byte limit

PostgreSQL truncates identifiers at 63 bytes silently. Archive names are built
to fit exactly: `retired-<alias>_<provisioned_at>_<digest>`, where the digest
is a `blake2s` of the **full** alias, so two aliases sharing a truncated prefix
cannot collide.

The separator is a hyphen deliberately. `ALIAS_RE` admits no hyphen, so an
archive name can never also be a registrable alias — otherwise an alias called
`retired_foo_20260101000000_deadbeef` would own precisely the server name a
later retirement of `foo` needed, and `foo` could never be retired.

The archived server name is **recorded**, not derived. The alias is truncated
four characters further for the server than for the schema, so
`archived_schema + "_srv"` is the wrong name for any alias long enough to
truncate — and a check against a name that cannot exist passes silently while
reading as coverage.

### Locking

Observe, Provision and `mark_unverifiable` each take
`pg_advisory_xact_lock` on `federation:observe:<alias>`, so two observations of
one alias cannot interleave — a reachable probe timestamps from the remote
clock and an unreachable one from the local clock, so they cannot be reconciled
after the fact by comparing times.

Retirement holds the derived-layer mutation lock across its whole operation, so
a derived layer cannot bind `source_<alias>` between the dependency check and
the DDL. It also holds the per-alias lock across its semantic update, so a
verification pass cannot mark a source available after retirement has
committed — retired aliases are excluded from later passes, so such a write
would stand indefinitely.

Observe holds its transaction open across the remote probe, which means the
probe is bounded by the transaction's own budget as well as per-statement
timeouts. The allowance is raised to 10 minutes for exactly that reason; a
probe that still exceeds it withdraws access rather than leaving the source
both unverified and authorised.

### Why archive rather than drop

The audit trail has to be inspectable in the catalogue, not merely asserted in
metadata a role can rewrite. `scripts/verify.sh` checks archived objects
directly: that the schema still exists under its recorded name, is owned by the
provisioner, and grants nothing to a consumer role; and that no server this
feature provisioned holds user mappings unless it belongs to a live alias.
That last invariant is stated over the set rather than per alias, so an
orphaned server or one renamed out of any recognisable shape is still caught.
