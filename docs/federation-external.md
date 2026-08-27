# Federating an external host database

[Federation](federation.md) attaches read-only sources to MAPP over
`postgres_fdw` so their relations can be queried, profiled, and joined into
derived layers alongside everything else. Under `MAPP_DATABASE_MODE=bundled`
it is already configured. Under `external` it is not, and this page is the
handoff for switching it on.

Read [external-postgresql.md](external-postgresql.md) first. This extends that
role model with a third role; it does not replace it.

## What you are being asked for

One additional login role on the **host** database — the server named by
`DBS_MAPP` — with two privileges the default handoff withholds:

| Privilege | Why |
| --- | --- |
| `USAGE` on foreign data wrapper `postgres_fdw` | To run `CREATE SERVER` and `CREATE USER MAPPING`. |
| `CREATE` on the database | To create the `federation` registry schema and one `source_<alias>` schema per attached source. |

The extension itself must also be installed, which requires a superuser once.

This is a genuine escalation over the derived owner and it should be read as
one. The list below is what the role can and cannot reach, so the decision can
be made on facts rather than on the word "federation".

**It can**: create, alter, and drop foreign servers and user mappings; create
and drop schemas named `federation` and `source_<alias>`; import foreign table
definitions into them; and grant the runtime reader and derived owner access
to those schemas.

**It cannot** read or write your existing schemas. It is granted nothing on
them, and MAPP never asks it to touch them. A foreign table is a pointer to
*another* server; creating one grants no access to anything local.

**Note carefully**: a user mapping stores the remote password in
`pg_user_mappings`, readable by the mapping's owner and by superusers. Anyone
who can already act as this role, or as a superuser on this database, can read
the credentials of every source you attach. Attach only sources whose
credentials you are willing to hold on this server. That is a property of
`postgres_fdw`, not of MAPP.

## Provisioning SQL

Continuing the example from [external-postgresql.md](external-postgresql.md) —
database `maps`, runtime reader `mapp_runtime_reader`, derived owner
`mapp_derived_owner`. Run as an administrator; set the password out of band.

```sql
-- Once per database, as a superuser.
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

-- Set the password out of band.
CREATE ROLE mapp_federation
  LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Same resource policy as the derived owner; `verify` applies one rule to
-- both. Every timeout must be set to a finite value, not left at its default.
ALTER ROLE mapp_federation CONNECTION LIMIT 4;
ALTER ROLE mapp_federation SET search_path = pg_catalog, public;
ALTER ROLE mapp_federation SET work_mem = '16MB';
ALTER ROLE mapp_federation SET hash_mem_multiplier = '1';
ALTER ROLE mapp_federation SET maintenance_work_mem = '64MB';
ALTER ROLE mapp_federation SET max_parallel_workers_per_gather = '2';
ALTER ROLE mapp_federation SET temp_file_limit = '1GB';
ALTER ROLE mapp_federation SET statement_timeout = '30min';
ALTER ROLE mapp_federation SET lock_timeout = '5s';
ALTER ROLE mapp_federation SET idle_in_transaction_session_timeout = '1min';

-- PostgreSQL 17 and later only.
ALTER ROLE mapp_federation SET transaction_timeout = '35min';

REVOKE TEMPORARY ON DATABASE maps FROM mapp_federation;
GRANT CONNECT ON DATABASE maps TO mapp_federation;

GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO mapp_federation;
GRANT CREATE ON DATABASE maps TO mapp_federation;

-- The registry. MAPP's bundled database creates this at initialisation; on an
-- external host it is yours to create, and `verify` fails with a message
-- naming this statement if it is missing.
CREATE SCHEMA federation AUTHORIZATION mapp_federation;
REVOKE ALL ON SCHEMA federation FROM PUBLIC;
```

Grant `mapp_federation` nothing else. It must not receive access to your source
schemas or to `derived_layers`, and must not be a member of any other role;
`verify` checks each of those and fails if the isolation has been broken.

Three settings are easy to omit and each fails verification:
`hash_mem_multiplier` defaults to `2` on PostgreSQL 14 and later where the
policy requires `1`; `temp_file_limit` defaults to unlimited; and `search_path`
must be exactly `pg_catalog, public`. On PostgreSQL 16 and earlier, omit
`transaction_timeout` as [external-postgresql.md](external-postgresql.md)
describes.

`GRANT CREATE ON DATABASE` is the privilege worth pausing on: it permits
creating *any* schema in that database, not only the ones MAPP uses.
PostgreSQL offers no narrower grant, and `verify` treats its absence as a
misconfigured provisioner rather than a hardened one — there is no supported
arrangement where an administrator pre-creates each `source_<alias>` schema
instead. If that escalation is unacceptable on this database, the supported
answer is to leave it unfederated.

`statement_timeout` is 30 minutes deliberately. Provisioning runs
`IMPORT FOREIGN SCHEMA` against a third-party server over the network, which is
slower than any interactive query and should not be cut off part-way.

## Configuration

Set both values in `.env` — one without the other is refused by `verify`:

```bash
FEDERATION_DATABASE_URL=postgresql://mapp_federation:...@your-host:5432/maps?sslmode=require
FEDERATION_DB_USER=mapp_federation
```

`./bin/mapp` picks up `compose.federation-external.yaml` automatically when
`FEDERATION_DATABASE_URL` is set under `external`, so `serve`, `verify`, and
`up` all resolve the same model. Setting the variable **is** the opt-in; there
is no second switch. Clearing it withdraws the credential at the next
`./bin/mapp serve`.

Invoking `docker compose` directly is the one case that needs the file named:

```bash
docker compose --project-directory . --env-file .env \
  --file compose.yaml --file compose.federation-external.yaml up -d
```

Omitting it recreates `config-ui` without the credential, which switches
federation off — the registry survives untouched in the database, but the API
reports `federation.not_configured` until the service is recreated with the
overlay.

## Confirming the grants took

Ask the host, rather than trusting the SQL above ran. The alias list needs a
token with `federation:observe` — the **Federation observer** preset grants
exactly that and nothing else:

```bash
curl -sS -H "Authorization: Bearer ${TOKEN}" \
  https://config.example/api/federation/aliases | python3 -m json.tool
```

The `host` object is read live from `pg_catalog` on each call, so it reflects
the grants as they are now rather than as they were at startup:

```json
{
  "aliases": [],
  "host": {
    "fdwInstalled": true,
    "canUseFdw": true,
    "canCreateSchemas": true,
    "registrySchemaPresent": true,
    "canUseRegistrySchema": true,
    "database": "maps",
    "role": "mapp_federation",
    "federationReady": true
  }
}
```

Read it against the SQL you ran: `fdwInstalled` is `CREATE EXTENSION`,
`canUseFdw` is the wrapper grant, `canCreateSchemas` is `GRANT CREATE ON
DATABASE`, and the two `registrySchema` fields are `CREATE SCHEMA federation`.
`federationReady` is the conjunction of the first three — it deliberately does
not include the registry schema, because that is reported separately by
`verify` with the statement needed to fix it.

A missing grant is not a hazard, only a refusal: `canUseFdw: false` means
`CREATE SERVER` raises `permission denied for foreign-data wrapper` and the
alias stays `pending`. Nothing is half-created, because provisioning runs in
one transaction.

## Withdrawing it

`POST /api/federation/aliases/{alias}/retire` revokes the consumer grants and
archives the objects; the registry row and its observation history stay for
audit. To remove the capability itself:

```sql
REVOKE CREATE ON DATABASE maps FROM mapp_federation;
REVOKE USAGE ON FOREIGN DATA WRAPPER postgres_fdw FROM mapp_federation;
```

Retire every alias first. Revoking while servers exist leaves foreign tables
that resolve to servers nobody can now alter, and dropping the role fails
outright while it owns objects.

---

## Why the credential, and not the mode

Until this change, `external` was refused outright. The reasoning was sound but
the mechanism was not: it inferred "MAPP administers this database" from
`MAPP_DATABASE_MODE`, which was reliable only while a local database and an
administered one were the same thing.

The mode never enforced anything. `compose.yaml` has never forwarded
`FEDERATION_DATABASE_URL`, so external deployments had no provisioner
regardless of the check — Compose was already the boundary. What the mode check
added was a refusal that could not be lifted by any legitimate means: an
operator who *had* provisioned the role correctly still got
`federation.not_configured`, with no way to proceed.

So the gate is now the credential, and the boundary is where it always was:
`compose.bundled-db.yaml` and `compose.federation-external.yaml` are the only
files that forward it, pinned by
`test_federation_provisioner_credential_is_opt_in`. Whether the grants behind
it are real is answered by the catalog, not by configuration.

## The pushdown gate

Attaching a source with PostGIS geometry is worth understanding before
measuring performance. `postgres_fdw` ships a predicate to the remote server
only for operators it is told are safe, which means the `postgis` extension
must be declared on the server *and* match on both sides. MAPP compares four
values — `postgis`, its `extversion`, `proj`, and `geos` — and declares PostGIS
shippable only if all four agree.

The cost of a mismatch is not an error; it is a plan that pulls every row back
before filtering. On the reference two-source demo the same spatial query
measured 658 ms with pushdown and 3143 ms without, against 419 ms for the same
data held locally. If a federated layer is unexpectedly slow, compare the
extension versions first.

Note also that federation requires `C` collation on collatable columns of an
exposed relation, checked at observation. `postgres_fdw` cannot ship a
comparison it cannot prove will order identically remotely, and a mismatched
collation is silently wrong rather than slow — hence a refusal rather than a
warning.
