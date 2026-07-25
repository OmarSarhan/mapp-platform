# External PostgreSQL administrator handoff

Use this document when MAPP will connect to a PostgreSQL database managed
outside this deployment. The database administrator remains responsible for
database availability, extensions, roles, grants, TLS, backups, and recovery.
MAPP does not create or migrate source schemas in external mode.

## Information the MAPP operator must provide

Before provisioning access, agree the following with the MAPP operator:

- database host, port, and database name;
- every source schema and relation that the workspace will use;
- the geometry column, geometry type, and positive SRID for each spatial
  relation;
- a stable, non-null feature identifier for each layer;
- whether managed derived layers are required;
- whether any derived query requires optional extensions such as H3; and
- the server CA, required client certificate, and `sslmode` policy.

Do not send passwords in tickets, command arguments, screenshots, or committed
configuration. Deliver them through the organizations' approved secret
channel.

## Required database features

- A supported PostgreSQL server reachable from the `xyz` and `config-ui`
  containers. `localhost`, loopback addresses, and the bundled hostname `db`
  are rejected in external mode.
- PostGIS installed in the target database.
- Source relations already populated and compatible with the workspace.
- PostgreSQL network policy, such as `pg_hba.conf` and firewalls, permitting
  TLS connections from the deployment.
- H3 extensions only when a proposed derived query uses H3 functions.

The application roles must not be superusers, database owners, source-schema
owners, members of privileged roles, or granted `BYPASSRLS`. If a source uses
row-level security, the database administrator must verify the rows visible to
both application roles; MAPP does not bypass that policy.

## Role model

Use two separate login roles when managed derived layers are enabled.

| Role | Application setting | Required access |
| --- | --- | --- |
| Runtime reader | `DBS_MAPP` | `CONNECT` to the database, `USAGE` on each approved source schema, and `SELECT` on each approved source relation. It also receives `USAGE` on `derived_layers` and `SELECT` on published derived outputs. |
| Derived owner | `DERIVED_DATABASE_URL` | `CONNECT` to the database, read-only access to approved source schemas and relations, and ownership of only the `derived_layers` schema. |

The runtime reader is shared by XYZ and configuration-service catalog and
validation reads. It must not receive `CREATE`, source DML, truncate, trigger,
or ownership privileges.

The derived owner is a privileged service credential, but only within
`derived_layers`. It creates, replaces, refreshes, and drops managed views and
materialized views through the configuration API. It must not receive write or
ownership privileges on any source schema. Submitted definitions are limited
to validated `SELECT` queries, but PostgreSQL privileges remain the underlying
security boundary.

If managed derived layers are not required, provision only the runtime reader,
leave `DERIVED_DATABASE_URL` and `DERIVED_READER_ROLE` empty, and skip all
`derived_layers` statements below.

## Example provisioning SQL

The following example uses:

- database `maps`;
- source schema `transport`;
- runtime reader `mapp_runtime_reader`; and
- derived owner `mapp_derived_owner`.

Run it as a database administrator after creating login secrets through the
administrator's normal secret-management process. Replace every example
identifier and grant only the relations approved for MAPP.

```sql
-- Set passwords out of band.
CREATE ROLE mapp_runtime_reader
  LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE mapp_derived_owner
  LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

GRANT CONNECT ON DATABASE maps TO mapp_runtime_reader;
GRANT CONNECT ON DATABASE maps TO mapp_derived_owner;

GRANT USAGE ON SCHEMA transport
  TO mapp_runtime_reader, mapp_derived_owner;

-- Prefer an explicit relation allowlist.
GRANT SELECT ON TABLE
  transport.bus_stops,
  transport.routes
  TO mapp_runtime_reader, mapp_derived_owner;

CREATE SCHEMA derived_layers AUTHORIZATION mapp_derived_owner;
REVOKE ALL ON SCHEMA derived_layers FROM PUBLIC;
GRANT USAGE ON SCHEMA derived_layers TO mapp_runtime_reader;
```

Repeat the `USAGE` and explicit `SELECT` grants for every approved source
schema and relation. PostgreSQL treats views and materialized views as tables
for `GRANT SELECT ON TABLE`.

Ordinary managed views use `security_invoker=true`, so the runtime reader must
retain direct `SELECT` access to their source relations. Granting access only
to the derived view is insufficient. The configuration service grants the
runtime reader `SELECT` on each successfully published derived output; it does
not grant access to the private `derived_layers._definitions` registry.

For a deliberately schema-wide policy, the database administrator may replace
the explicit allowlist with:

```sql
GRANT SELECT ON ALL TABLES IN SCHEMA transport
  TO mapp_runtime_reader, mapp_derived_owner;

ALTER DEFAULT PRIVILEGES FOR ROLE source_owner IN SCHEMA transport
  GRANT SELECT ON TABLES TO mapp_runtime_reader, mapp_derived_owner;
```

`ALTER DEFAULT PRIVILEGES` affects only objects subsequently created by the
named `source_owner`; repeat it for every role that creates source relations.
It is broader than an explicit allowlist and should be used only when all
present and future relations in that schema are approved for MAPP.

If the server has revoked the usual public execution rights on PostGIS or H3
functions, grant `USAGE` on the extension schema and `EXECUTE` only on the
functions required by the configured layers and reviewed derived queries.

## Application configuration

The MAPP operator stores the connection URIs in the deployment's private
environment file:

```dotenv
MAPP_DATABASE_MODE=external
DBS_MAPP=postgresql://mapp_runtime_reader:PERCENT_ENCODED_PASSWORD@postgres.example.org:5432/maps?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt
DERIVED_DATABASE_URL=postgresql://mapp_derived_owner:PERCENT_ENCODED_PASSWORD@postgres.example.org:5432/maps?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt
DERIVED_READER_ROLE=mapp_runtime_reader
```

`DERIVED_READER_ROLE` is a PostgreSQL role identifier, not a URI. It must name
the runtime role from `DBS_MAPP` so the service can grant that role access to
published outputs.

Percent-encode URI-reserved characters in usernames and passwords. For private
certificate authorities or client certificates, both application images need
reviewed read-only mounts at the same absolute paths used in their connection
URIs. Do not weaken TLS verification to compensate for a missing certificate.

## Administrator verification

Before handing the credentials to the MAPP operator, verify the effective
permissions using the real database, schemas, and representative relations:

```sql
SELECT has_database_privilege(
  'mapp_runtime_reader', 'maps', 'CONNECT'
);
SELECT has_schema_privilege(
  'mapp_runtime_reader', 'transport', 'USAGE'
);
SELECT has_table_privilege(
  'mapp_runtime_reader', 'transport.bus_stops', 'SELECT'
);
SELECT has_schema_privilege(
  'mapp_runtime_reader', 'transport', 'CREATE'
); -- must be false

SELECT has_database_privilege(
  'mapp_derived_owner', 'maps', 'CONNECT'
);
SELECT has_table_privilege(
  'mapp_derived_owner', 'transport.bus_stops', 'SELECT'
);
SELECT has_schema_privilege(
  'mapp_derived_owner', 'transport', 'CREATE'
); -- must be false

SELECT nspowner::regrole = 'mapp_derived_owner'::regrole
FROM pg_namespace
WHERE nspname = 'derived_layers'; -- must be true
```

Also connect as each role and confirm that representative permitted `SELECT`
queries succeed, source writes and source-schema creation fail, and the runtime
reader cannot create objects in or inspect the private registry under
`derived_layers`.

The MAPP operator must then run:

```sh
./bin/mapp doctor
./bin/mapp config
./bin/mapp serve
./bin/mapp verify
```

The generic verifier is not proof that every workspace layer renders
correctly. Complete acceptance with catalog inspection and post-start visual
tests covering representative point, line, polygon, and derived layers. Record
database and visual evidence without recording credentials or connection
strings.

## Ongoing administration

- Preserve the grants when source relations are replaced or ownership changes.
- Review access whenever the workspace adds a schema, relation, or extension.
- Monitor expensive ordinary views and materialized refreshes.
- Back up `derived_layers` with the external database if its definitions and
  materialized results are required for recovery.
- Rotate both login secrets through the approved deployment procedure.
- Re-run permission and visual checks after database migrations, role changes,
  PostGIS upgrades, or restore operations.
