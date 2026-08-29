# MAPP Platform: a guide

This is the document to read first. It starts with what MAPP is and how to see
it working, then adds detail as it goes: by the end you will know how to attach
your own data, what the platform knows about it, and how to operate it safely.

Every section links to a reference document where the detail lives. Read those
when you need them, not before.

## Contents

1. [What MAPP is](#1-what-mapp-is)
2. [Your first twenty minutes](#2-your-first-twenty-minutes)
3. [The mental model](#3-the-mental-model)
4. [Federation: attaching a source](#4-federation-attaching-a-source)
5. [Semantics: what the platform knows about a relation](#5-semantics-what-the-platform-knows-about-a-relation)
6. [Derived layers](#6-derived-layers)
7. [The workspace and the map](#7-the-workspace-and-the-map)
8. [Operating it](#8-operating-it)
9. [The security model](#9-the-security-model)
10. [Reference index](#10-reference-index)

---

## 1. What MAPP is

MAPP publishes maps from PostgreSQL data that lives somewhere else.

You point it at one or more PostgreSQL databases you already have. It attaches
them read-only, records what it knows about each relation, lets you build
aggregated layers across them, and serves the result as a map through a pinned
GEOLYTIX XYZ build.

```
  your PostgreSQL databases                    MAPP
  ─────────────────────────                    ────
   census-db ──┐
               ├── read-only ──▶  packaged PostgreSQL ──▶  XYZ  ──▶  the map
   ops-db ─────┘   postgres_fdw   ├─ derived layers
                                  ├─ federation registry
                                  └─ semantic catalogue
```

The single most important thing to understand: **MAPP does not hold your
spatial data.** It packages one PostgreSQL database, and that database holds
only the platform's own state — the layers it derives, the registry of sources
it has attached, and what it knows about their columns. Your data stays in your
databases and is read across the boundary.

**What it is not.** It is not a data warehouse, and it is not an ETL tool for
loading your data into it. There is no packaged ETL. If you want data in MAPP,
you attach the database that holds it.

## 2. Your first twenty minutes

You need Docker with Compose, and roughly 4 GB of free disk. Everything runs in
containers; nothing is installed on the host.

```sh
cd mapp-platform
./bin/mapp init --demo     # write .env, generate secrets, turn the demo on
./bin/mapp all             # build and start the platform, then verify it
./bin/mapp demo            # load two real open-data sources and publish a map
```

`init` writes a private `.env` and generates every secret in it. `--demo` adds
two throwaway source databases to the deployment — this is the one flag that
makes the platform stand up databases of its own, and it exists so you have
something real to look at.

`demo` takes about fifteen minutes, most of it downloading the England Census
2021 Output Area dataset. It is doing four separate things, and the output
names each one:

| Step | What happens |
| --- | --- |
| Loading sources | Two source databases are populated straight from their publishers: Leeds City Council's ArcGIS feeds into `ops-db`, ONS Census 2021 into `census-db` |
| Registering and provisioning | Both are attached to MAPP as federated sources |
| Profiling and describing | Each exposed relation is profiled, then described by a model |
| Building and publishing | Two derived layers are computed across both sources and put on the map |

Then open:

- the map — <http://localhost:3000>
- the configuration dashboard — <http://config.localhost:3000>

The dashboard password was printed by `init`. If you have lost it,
`./bin/mapp reset-config-password` issues a new one.

**What you just built.** Two independent PostgreSQL servers holding real open
data, attached read-only to MAPP, with a population layer and a smoke-control
layer computed by intersecting one source's polygons with the other's census
geography — a join across two separate databases, aggregated into H3 cells.

If anything goes wrong at this stage, the answer is almost always to run
`./bin/mapp demo` again. It is idempotent, and a broken demo is rebuilt rather
than repaired.

## 3. The mental model

### Two kinds of database

**The packaged database** is MAPP's own. One per deployment, always present,
started by Compose. It holds three schemas that matter:

- `derived_layers` — the layers MAPP computes, and the definitions that produce them
- `federation` — the registry of attached sources, their observation history, and group labels
- `semantic` — the catalogue of what MAPP knows about every relation it can see

It also carries PostGIS and H3, because derived work runs there.

**Source databases** are yours. MAPP never writes to them. It connects with a
read-only role you provide and attaches the relations you explicitly allow.

Everything else — dashboard authentication, the audit log, workspace
proposals, artifacts — lives in files under `var/`, not in the database.

### Why the split matters

It gives you one clean boundary. The packaged database can be reset,
rebuilt or restored without touching your data, and your databases can be
backed up and governed by whoever already owns them. It also means that when
you attach two sources, they become foreign tables in the *same* database — so
a query can join across them, which is what makes the demo's cross-source layer
possible at all.

Read: [`architecture.md`](architecture.md)

## 4. Federation: attaching a source

Attaching a source is four deliberate steps, and they are separate because
each one grants something different.

```
  register  ──▶  observe  ──▶  provision  ──▶  (retire)
  records        probes it     serves data      withdraws it
  intent         live
```

**Register** records your intent to attach a source: its alias, the connection
reference, the relations you will allow, and how the data may be handled. It
opens no connection and exposes nothing. Scope: `federation:register`.

**Observe** connects to the source and records what it found — reachability,
the schema fingerprint, the physical identity of the server. This is the first
moment MAPP talks to a third-party database, which is why it needs the higher
scope. Scope: `federation:provision`.

**Provision** is the only step that serves data. It creates a `source_<alias>`
schema of foreign tables and grants the runtime and derived roles read access.
It re-verifies everything live first and refuses if the source has changed
identity since you looked. Scope: `federation:provision`.

**Retire** withdraws a source. It archives rather than drops: the objects are
renamed out of the way and the history is kept, so the audit trail stays
physically inspectable.

From the CLI:

```sh
config-cli federation register mysource --connection-ref MYSOURCE \
  --relation public.sites --data-handling "Open data, OGL v3." \
  --acknowledge-data-handling
config-cli federation observe mysource
config-cli federation provision mysource --expected-observation-id 7 --confirm
```

### Groups are labels

A group is a name and a description. A source belongs to zero or more. It
grants nothing, revokes nothing, and changes no privilege — it records which
sources are *meant* to be used together.

That is deliberate, and the reason is worth knowing: every provisioned source
is already a foreign table in the one packaged database, so a join across any
two of them works with no group concept at all. Grouping cannot enable
cross-source querying. It could only restrict it, and a source in two groups
bridges them anyway.

### When a source goes away

A background pass re-observes every provisioned source. When one becomes
unreachable, MAPP withdraws consumer access, marks the alias `unavailable`, and
marks its semantic profiles as having an unusable source. Planning then refuses
to build anything from them, naming the source rather than failing later on a
permission error.

None of that is destructive. The profiles stay, and when the source comes back
the next pass restores access and everything becomes usable again.

Read: [`federation.md`](federation.md)

## 5. Semantics: what the platform knows about a relation

Exposing a relation and understanding it are separate decisions, with separate
permissions.

**Profiling** reads a relation's structure — columns, types, keys, geometry —
and records it as a *generated* profile. No row values are read. This is what
`semantic:source` allows.

**Curating** adds human meaning on top: a description, display names, caveats.
It never overwrites the generated facts; the two are stored separately, so a
re-profile after a schema change cannot silently discard what somebody wrote.

**Generating** asks a model to draft that curated meaning. It produces a
*draft* and persists nothing. To reach the catalogue a draft must be checked,
proposed and applied — three explicit steps, so nothing a model wrote lands
without somebody agreeing to it.

```
  profile  ──▶  generate  ──▶  check  ──▶  propose  ──▶  apply
  (structure)   (a draft)      (validate)  (a record)    (it lands)
```

### The data context decision

By default a draft is written from metadata alone: column names, types, the
relation name. You can optionally send **sample rows** and **column
statistics** too, which produces far better descriptions and requires the
separate `semantic:data` scope.

That default is off because it is a judgement about the data in front of you.
`./bin/mapp demo` turns it on, because its sources are published government
open data and the judgement is easy there.

Read: [`semantic-layer.md`](semantic-layer.md)

## 6. Derived layers

A derived layer is a relation MAPP computes and owns, in the `derived_layers`
schema. The platform tracks its definition, refreshes it on request, and
refuses to drop anything the workspace still points at.

The recipe the demo uses is **area-weighted H3 allocation**: take polygons from
one source, intersect them with a census geography from another, weight each
measure by the proportion of area that overlaps, and aggregate into H3 cells at
a chosen resolution.

Two things are worth knowing before you build one:

- **It needs a ready semantic profile.** Planning refuses a source it has no
  profile for, and refuses one whose source is currently unavailable. That is
  the check doing its job, not an obstacle.
- **Intersect in the source's own SRID.** Transforming the indexed side inside
  the join makes the spatial index unusable and turns seconds into minutes.

Read: [`derived-layers.md`](derived-layers.md)

## 7. The workspace and the map

The workspace is the JSON document XYZ renders: locales, layers, styles,
filters. MAPP does not let you edit it in place. You build a **proposal**,
preflight it, and apply it — so every change to what the map shows is a
reviewable record.

`./bin/mapp demo` does exactly this at its final step, which is why it prints
`apply: ok` rather than writing a file.

Read: [`workspace-schema.md`](workspace-schema.md)

## 8. Operating it

### Verify

```sh
./bin/mapp verify
```

The single most useful command. It checks service health, every database
identity and its privileges, the catalogue, tiles, and the gateway guards — and
it connects through each service's actual credentials rather than trusting the
names in `.env`. Run it after any change you are unsure about.

### Test

```sh
./bin/mapp test
```

Unit and contract suites for the platform, the semantic service, the ETL and
the deployment helpers, plus the frontend. It provisions its own scratch
database, so it needs Docker but not a running deployment.

### Backup

The packaged database is one PostgreSQL volume: back it up with `pg_dump` and
you have the derived layers, the federation registry and the whole semantic
catalogue. Back up `var/` alongside it for authentication, audit and
proposals. Your source databases are yours and are not part of this.

Read: [`backup-restore.md`](backup-restore.md)

### Reset

```sh
./bin/mapp reset-data --confirm
```

Removes the packaged database entirely and restores the seed workspace. Read
the warning it prints: it destroys derived layers, the federation registry and
the semantic catalogue, and those do not come back. Your source databases are
untouched, so `./bin/mapp demo` rebuilds the showcase from them afterwards.

### Production

Production is an explicit environment setting, and it refuses configurations
that are fine in development — the demo sources among them.

Read: [`deployment.md`](deployment.md), whose
[production acceptance](deployment.md#production-acceptance-evidence) section covers the
evidence a release is expected to carry.

## 9. The security model

Every component gets its own PostgreSQL role, and each role is the narrowest
thing that can do its job.

| Role | Used by | Can do |
| --- | --- | --- |
| `mapp_xyz` | XYZ, the map renderer | Read the workspace's layers |
| `mapp_derived` | Derived-layer work | Own and build `derived_layers`; read provisioned sources |
| `mapp_federation` | The provisioner | Own `federation` and the `source_<alias>` schemas; use `postgres_fdw` |
| `mapp_semantic` | Semantic service, writes | Own `semantic` and nothing else |
| `mapp_semantic_reader` | Semantic service, reads | `SELECT` on `semantic` and nothing else |

None of them is a superuser. None can create databases or roles. `verify`
audits all of it, including that the read-only role genuinely cannot write —
because an audit that only checks it can read proves nothing.

**Tokens and scopes.** The dashboard and CLI authenticate with scoped tokens.
The scopes are narrow on purpose: reading the federation registry, attaching a
source, and serving its data are three different permissions, and the one that
opens a live outbound connection to a third-party database is the one to be
careful with.

Read: [`security.md`](security.md)

## 10. Reference index

**Start here**

| Document | What it covers |
| --- | --- |
| [`architecture.md`](architecture.md) | Components, networks, and how they fit together |
| [`deployment.md`](deployment.md) | Standing it up for real, including HTTPS |
| [`operations.md`](operations.md) | Day-to-day running, loading data, troubleshooting |

**Working with data**

| Document | What it covers |
| --- | --- |
| [`federation.md`](federation.md) | The full source lifecycle, error codes, traps |
| [`semantic-layer.md`](semantic-layer.md) | Profiles, curation, generation, delivery |
| [`derived-layers.md`](derived-layers.md) | Recipes, refresh, dependency guards |
| [`workspace-schema.md`](workspace-schema.md) | The workspace document XYZ renders, the audited plugin surface, and the pinned XYZ field audit |
| [`external-postgresql.md`](external-postgresql.md) | Preparing a source database to be attached |

**Operating and assuring**

| Document | What it covers |
| --- | --- |
| [`security.md`](security.md) | Roles, isolation, secrets, the trust boundary |
| [`backup-restore.md`](backup-restore.md) | What to back up and how to restore it |
| [`supply-chain.md`](supply-chain.md) | Pinned images, package remediation, XYZ framework policy |
| [`api-contract.md`](api-contract.md) | Every route, its scope and its shape |

---

**Working on MAPP rather than with it?** [`../CONTRIBUTING.md`](../CONTRIBUTING.md)
covers the development container, the suites and the review expectations.
