# MAPP Platform

MAPP Platform is the deployable server half of the project. It runs a pinned
GEOLYTIX XYZ build against PostgreSQL/PostGIS, the workspace configuration
dashboard and API, server-side browser validation, and Caddy. An optional
Leeds ArcGIS container can populate the bundled database with sample data.

The remote [`config-cli`](https://github.com/OmarSarhan/mapp-config-cli) is a
separate project. It is installed on an operator or AI-agent computer and
communicates with this platform over the authenticated configuration API; it
is not bundled into the platform image or source tree.

This directory is repository-ready source, not proof of a
history-preserving Git split. Publish it only after repeating the separation
from the canonical clone, retaining relevant history and tags, and scanning
the complete history as described in
[Repository split](docs/repository-split.md).

```text
optional Leeds ETL ──> bundled sample PostGIS ─┐
                                               ├─ DBS_MAPP ─┬─> XYZ
external operator-managed PostGIS ─────────────┘            └─> config API

browser ── HTTPS ──> Caddy ──> XYZ
                           └─> config API <── standalone config-cli
                                               on a separate computer
```

## Repository and state layout

The repository separates reviewed deployment inputs from mutable live state:

```text
instance/                         versioned deployment inputs
├── workspace.seed.json          initial workspace only
├── xyz.env                      non-secret XYZ runtime settings
├── etl/layers.json              ETL source and field manifest
└── public/
    ├── svg/                     public custom map icons
    └── plugins/                 trusted manifest-backed XYZ plugins

var/                              ignored mutable runtime state
├── workspace/
│   ├── workspace.json           authoritative live workspace
│   └── workspace.json.bak       previous atomic-save version
├── control/
│   ├── auth.json                authentication, session, token, and device state
│   ├── audit.jsonl              security and change audit
│   ├── proposals/               revision-bound proposal lifecycle records
│   ├── operations/              durable bounded long-action records
│   └── artifacts/               visual reports and screenshots
├── preview/
│   └── workspace.json           private proposal-preview workspace
├── reload/                       narrow live XYZ reload coordination
└── preview-reload/               isolated preview XYZ reload coordination
```

`instance/workspace.seed.json` is copied to the live workspace only when no
live workspace exists. Normal dashboard or API saves update
`var/workspace/workspace.json`; they do not rewrite the seed. Treat `var` as
host-owned operational data: do not commit it, include it in container build
contexts, or place it under XYZ's general file-resource path.

The intended mount boundary is:

- XYZ: live workspace read-only, `instance/public` read-only, reload channel
  read/write.
- XYZ preview: private preview workspace read-only, `instance/public`
  read-only, preview reload channel read/write.
- Configuration service: live and preview workspaces, control state, and both
  reload channels read/write; public SVGs read-only.
- Browser runner: only `var/control/artifacts` read/write.
- ETL: only `instance/etl` read-only.

See [Architecture](docs/architecture.md) and [Security](docs/security.md) for
the complete trust boundary.

## Quick start

Docker Engine, Docker Compose v2, Python 3, and OpenSSL are required. The test
entry point runs frontend and component checks in Docker; a host Node
installation or project-level npm install is not required.

For isolated development, open this repository directory (not its parent split
workspace) in VS Code and choose **Dev Containers: Reopen in Container**. The
repository-local container runs its own Docker daemon and publishes the MAPP
gateway on host port `3000`; it does not mount the host Docker socket. Keep the
standalone CLI open in its own repository and dev container.

```sh
./bin/mapp init
./bin/mapp serve
./bin/mapp etl bus_stops
./bin/mapp etl definitive_paths
```

Initialization creates a private `.env`, initializes control-plane
authentication, and creates the live workspace from the versioned seed when
needed. It must preserve existing `.env`, database, workspace, audit, proposal,
and artifact state.

Development-only local defaults:

- Map: <http://localhost:3000>
- Configuration dashboard: <http://config.localhost:3000>

`./bin/mapp serve` starts the long-running services without running the sample
ETL. In bundled-database mode, `./bin/mapp etl` loads every sample layer and
`./bin/mapp etl bus_stops` selects one configured sample layer.

The broken recent-planning sample was replaced with Leeds Smoke Control Orders,
a healthy bounded polygon source. The replacement has its own table and
workspace mapping; the loader deliberately leaves any old
`leeds.planning_applications_recent` table untouched. Expected ArcGIS service
failures now produce a concise error, preserve deletion safety, and still
return non-zero for automation. See the
[ETL polygon-source note](etl/README.md#polygon-source-selection).

## HTTPS-first live deployment

Caddy is the only public endpoint, and HTTPS is the primary live-server
topology. Persist the production choice and public origins in the private
`.env` rather than relying on a shell export:

```dotenv
MAPP_ENVIRONMENT=production
EDGE_BIND_ADDRESS=0.0.0.0
HTTP_PORT=80
HTTPS_PORT=443
PRODUCTION_MAP_SITE=https://maps.company.co.uk
PRODUCTION_CONFIG_SITE=https://config.company.co.uk
PRODUCTION_CONFIG_ALLOWED_HOSTS=config.company.co.uk,config-ui
PRODUCTION_CADDY_EMAIL=operations@company.co.uk
```

Point both DNS names at the server and allow inbound TCP 80 and TCP/UDP 443.
Caddy obtains and renews the certificates, redirects ordinary HTTP traffic to
HTTPS, and retains its ACME state in the named Caddy volumes. Port 80 is kept
only for redirect and certificate automation; XYZ and the configuration
service remain unpublished behind Caddy.

Run `./bin/mapp config` before `./bin/mapp serve`; production validation rejects
HTTP origins, loopback binding, non-standard public ports, placeholder domains,
an unmonitored ACME email, and root application IDs. Initialize and operate the
deployment as a dedicated unprivileged host account. Local
`http://localhost:3000` remains available
only when `MAPP_ENVIRONMENT=development`. The two topology keys
`MAPP_ENVIRONMENT` and `MAPP_DATABASE_MODE` are authoritative in the selected
env file; conflicting shell exports are rejected. Use `MAPP_ENV_FILE` to select
a different reviewed env file. Conflicting exported database connection,
role, and password variables are also rejected so Compose cannot silently
replace the reviewed database target. See [Deployment](docs/deployment.md) for
firewall, backup, and acceptance requirements.

## Database configuration

All database routing is consolidated in the private `.env`. XYZ and the
configuration dashboard receive the exact same `DBS_MAPP` connection string,
which corresponds to `"dbs": "MAPP"` in the workspace. This prevents the
dashboard from validating against a different database from the one XYZ uses.

| Variable | Purpose |
| --- | --- |
| `MAPP_DATABASE_MODE` | `bundled` starts the included PostGIS sample database; `external` starts only the platform services. |
| `DBS_MAPP` | PostgreSQL URI used by both XYZ and the configuration dashboard. Replace the complete URI for an external PostGIS server. |
| `POSTGRES_DB` | Database name created by the bundled database overlay and referenced by its default connection strings. |
| `XYZ_DB_USER`, `XYZ_DB_PASSWORD` | Read-only role used by the default bundled `DBS_MAPP`. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | Bootstrap administrator for the bundled sample database only. They are not passed to XYZ or the dashboard. |
| `ETL_DB_USER`, `ETL_DB_PASSWORD` | Writer role for the optional bundled sample ETL only. |
| `ETL_DATABASE_URL` | Separate ETL destination; it is never used by XYZ or the dashboard. |
| `DB_BIND_ADDRESS`, `DB_PORT` | Optional host publication of the bundled database through `compose.db-port.yaml`; these do not select an external server. |

The default created by `./bin/mapp init` is the bundled sample arrangement:

```dotenv
MAPP_DATABASE_MODE=bundled
DBS_MAPP=postgresql://${XYZ_DB_USER}:${XYZ_DB_PASSWORD}@db:5432/${POSTGRES_DB}?sslmode=disable
ETL_DATABASE_URL=postgresql://${ETL_DB_USER}:${ETL_DB_PASSWORD}@db:5432/${POSTGRES_DB}?sslmode=disable
```

Use `./bin/mapp all` to load and verify every configured bundled sample source.

To remove the complete bundled PostgreSQL volume and rebuild it with only the
configured ETL sources, run:

```sh
./bin/mapp reset-data --confirm
```

This deletes derived layers and every other non-ETL database object. It
replaces the live and preview workspaces with `instance/workspace.seed.json`,
clearing layer configuration that depended on deleted data. Dashboard
authentication, audit, proposal, artifact, and public-asset state is preserved.
The command is unavailable in external-database mode and requires the explicit
`--confirm` guard.
Source availability can change independently; treat a non-zero ETL exit as a
failed refresh and inspect the recorded run before retrying.

To use an externally managed PostGIS database, replace the complete runtime
URI and change the mode:

```dotenv
MAPP_DATABASE_MODE=external
DBS_MAPP=postgresql://mapp_reader:PERCENT_ENCODED_PASSWORD@postgis.internal.example:5432/production_maps?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt
```

When converting an existing bundled installation, take a database backup and
run `./bin/mapp down` before changing the mode. This removes the old service
topology without deleting its named PostgreSQL volume; external mode does not
start or automatically remove a previously running bundled `db` container.

Then use:

```sh
./bin/mapp doctor
./bin/mapp config
./bin/mapp serve
./bin/mapp verify
```

In external mode the wrapper does not start the bundled `db` service and
rejects `etl`, `all`, and `db`, preventing the sample loader and local database
tools from being mistaken for production operations. It also rejects the
bundled hostname `db`, localhost names, and loopback addresses in `DBS_MAPP`.
The external server must:

- be reachable from containers on the Compose backend network; do not use
  `localhost`, which would mean the individual application container;
- have PostGIS installed;
- grant the URI role `CONNECT` on the database, `USAGE` on every mapped schema,
  and `SELECT` on the relations used by the workspace; and
- expose geometry columns, SRIDs, primary/feature identifiers, and calculated
  fields compatible with the configured workspace layers.

Percent-encode URI-reserved characters in database usernames and passwords.
Choose an appropriate PostgreSQL `sslmode`; production external connections
should normally validate TLS rather than use the bundled default of `disable`.
The example explicitly points both database clients at the system CA bundle
present in both application images. This is needed because Node `pg` and
libpq/psycopg do not otherwise use the same default root-certificate path. A
private CA or client certificate needs a reviewed read-only mount at the same
absolute path in both `xyz` and `config-ui`, plus matching PostgreSQL URI
parameters; do not weaken verification to work around missing trust material.

Keep `.env` mode `0600`, outside version control, logs, screenshots, and support
messages.

Changing `DBS_MAPP` changes the database, not the layer definitions. Before the
first initialization, edit `instance/workspace.seed.json` for the external
schemas, tables, geometry columns, and identifiers. For an existing instance,
the authoritative configuration is `var/workspace/workspace.json`; update it
through the dashboard or a revision-bound `config-cli` proposal. The ArcGIS ETL
manifest remains separate under `instance/etl/layers.json` and is irrelevant to
normal external-database operation.

For production, do not rely on the local defaults. Read
[Deployment](docs/deployment.md), [Security](docs/security.md), and
[Backup and restore](docs/backup-restore.md) first. The shipped direct
production topology requires distinct public HTTPS DNS hostnames for map and
configuration traffic, with Caddy directly bound to ports 80 and 443.

## Configuring the workspace

The dashboard edits the live workspace through server-side validation. It
discovers PostGIS relations visible to the read-only XYZ role, validates
geometry and feature identifiers, checks calculated information expressions,
and runs a bounded render probe before saving. Every successful dashboard save
atomically replaces the live workspace, requests an XYZ restart, and waits for
the XYZ supervisor to report TCP readiness with the exact saved workspace
fingerprint. The dashboard shows the restart in progress and then confirms
that connection readiness; operators do not need to issue a second reload.

The top-level `locale` remains XYZ's default rendered locale even when
`locales` is present. XYZ composes that default into each named locale except a
named key literally called `locale`, because that name resolves the top-level
default rather than a distinct alternative. XYZ's rules include conditional
array concatenation/replacement and are not equivalent to a generic deep
merge. The dashboard, API, and CLI select the top-level default when no name is
requested and resolve named alternatives with the same composition semantics.
If raw `workspace.locale` is absent, XYZ synthesizes an empty
`{"layers": {}}` default; neither an omitted locale nor the name `locale`
auto-selects a sole named alternative.
Because a composed value may be inherited from several raw properties, named
effective locales are inspectable in the dashboard and testable through the
server API/CLI, but read-only in dashboard controls. Use focused
`config-cli`/API proposal operations against the raw named override to edit one
without flattening inherited content.

XYZ also supports external renderers, templates, inline features, zoom-keyed
tables/geometries, icon arrays, and named style references. The platform
preserves those advanced forms. The dashboard keeps their ordinary
database-specific controls read-only and exposes their complete JSON for
expert editing, because they cannot be represented safely as one catalog
relation. When such a layer is viewed through a composed named locale, its
entire dashboard editor remains read-only under the named-locale rule above.

Use the dashboard for interactive administration. Use the separately installed
`config-cli` for remote, JSON-first automation:

1. Inspect the server identity, contract, current revision, layer, schema,
   rules, and catalog.
2. Create the smallest revision-bound proposal.
3. Present the explanation, focused diff, warnings, and visual evidence.
   Top-level visual commands inspect the current live workspace; when the
   server advertises proposal preview commands, use them to render the stored
   pending candidate in the isolated preview process before approval.
4. Apply only after explicit approval. A successful apply automatically
   requests and waits for the same fingerprint-matched XYZ reload.
5. Check the returned XYZ reload status and run a post-apply visual test.

Do not directly edit a remote `workspace.json`. The platform API is the remote
write boundary, records proposal and audit state, and is what triggers the
managed reload. Direct filesystem edits are intentionally not watched. Prefer
scoped, expiring device credentials for agents; legacy full tokens remain
available for operators and migration as documented in
[Security](docs/security.md).

The public custom SVG catalog is versioned under
[`instance/public/svg`](instance/public/svg). SVGs are exposed as
`/instance/svg/<filename>.svg` after bounded safety checks.

The machine-readable workspace schema is
[`config-ui/schema/workspace.schema.json`](config-ui/schema/workspace.schema.json).
See [Workspace schema](docs/workspace-schema.md) and the
[XYZ field audit](docs/xyz-workspace-field-audit.md).

## XYZ framework policy

This repository does not vendor or alter the GEOLYTIX XYZ framework. The XYZ
Dockerfile clones the configured upstream release, verifies its full commit,
builds it, and layers only the deployment supervisor and instance mappings
around it. Upgrade work should change the pinned reference and commit, build a
new image, and verify the platform; it should not patch the framework source in
this repository.

Because the upstream installation is not fully dependency-locked, an accepted
production image should be retained and deployed by immutable digest. That
release hardening is still outstanding.

## Common commands

```sh
./bin/mapp serve
./bin/mapp etl bus_stops
./bin/mapp test
./bin/mapp doctor
./bin/mapp verify
./bin/mapp ps
./bin/mapp logs
./bin/mapp db
./bin/mapp reload-xyz
./bin/mapp reset-config-password
./bin/mapp stop
./bin/mapp down
```

`down` removes containers and networks but is intended to preserve the named
PostgreSQL and Caddy volumes. Always take a backup before database image,
schema, or deployment-boundary changes.

`reset-config-password` generates and displays a new configuration-dashboard
administrator password once, invalidating existing dashboard sessions without
restarting the service. It does not revoke CLI tokens or change database
credentials. See [Credentials](docs/operations.md#credentials) for recovery,
custom environment-file, and token-revocation instructions.

## Verification

Available checks include:

```sh
./bin/mapp test
./bin/mapp doctor
./bin/mapp config
./bin/mapp verify
```

`test` builds controlled component images, runs the component Python suites,
frontend tests/build/audit and JavaScript syntax checks in containers, then
runs standard-library wrapper/production helper tests and Compose validation
from the host. `doctor` compares environment key names without printing their
values. The last command is a mode-aware runtime check and requires the stack,
the selected PostGIS connection, and at least one discoverable relation. In
bundled mode it additionally verifies the sample ETL relations and tile; in
external mode it verifies generic connectivity, catalog, service, and gateway
gates. Finish external acceptance with layer-specific visual tests for that
workspace.
Dated results and their exact scope are recorded in
[`docs/validation-log.md`](docs/validation-log.md). Treat only the checks
explicitly recorded there as evidence; source restructuring alone is not an
acceptance result.

## Documentation

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [External PostgreSQL administrator handoff](docs/external-postgresql.md)
- [Operations](docs/operations.md)
- [Security](docs/security.md)
- [Backup and restore](docs/backup-restore.md)
- [Configuration API contract](docs/api-contract.md)
- [Repository split](docs/repository-split.md)
- [Workspace schema](docs/workspace-schema.md)
- [Validation history](docs/validation-log.md)
- [Contributing](CONTRIBUTING.md)
- [Security reporting](SECURITY.md)
- [Licensing status](LICENSING.md)

The project does not yet declare a project-level licence. Do not assume that
the source or bundled assets may be redistributed until the owner completes
the decisions listed in [`LICENSING.md`](LICENSING.md).
