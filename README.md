# MAPP Platform

MAPP Platform is the deployable server half of the project. It runs a pinned
GEOLYTIX XYZ build with PostgreSQL/PostGIS, a Leeds ArcGIS ETL, the workspace
configuration dashboard and API, server-side browser validation, and Caddy.

The remote `config-cli` is a separate project. It is installed on an operator
or AI-agent computer and communicates with this platform over the authenticated
configuration API; it is not bundled into the platform image or source tree.

This directory is repository-ready source, not proof of a
history-preserving Git split. Publish it only after repeating the separation
from the canonical clone, retaining relevant history and tags, and scanning
the complete history as described in
[Repository split](docs/repository-split.md).

```text
Leeds ArcGIS REST ──> ETL ──> PostGIS <── XYZ
                                  ^          ^
                                  │          │
                         config dashboard   live workspace

browser ──> Caddy ──> XYZ
                    └─> configuration API <── standalone config-cli
                                              on a separate computer
```

## Repository and state layout

The repository separates reviewed deployment inputs from mutable live state:

```text
instance/                         versioned deployment inputs
├── workspace.seed.json          initial workspace only
├── xyz.env                      non-secret XYZ runtime settings
├── etl/layers.json              ETL source and field manifest
└── public/svg/                  public custom map icons

var/                              ignored mutable runtime state
├── workspace/
│   ├── workspace.json           authoritative live workspace
│   └── workspace.json.bak       previous atomic-save version
├── control/
│   ├── auth.json                password/session/token hashes
│   ├── audit.jsonl              security and change audit
│   ├── proposals/               revision-bound proposal lifecycle records
│   └── artifacts/               visual reports and screenshots
└── reload/                       narrow XYZ reload coordination
```

`instance/workspace.seed.json` is copied to the live workspace only when no
live workspace exists. Normal dashboard or API saves update
`var/workspace/workspace.json`; they do not rewrite the seed. Treat `var` as
host-owned operational data: do not commit it, include it in container build
contexts, or place it under XYZ's general file-resource path.

The intended mount boundary is:

- XYZ: live workspace read-only, `instance/public` read-only, reload channel
  read/write.
- Configuration service: live workspace, control state, and reload channel
  read/write; public SVGs read-only.
- Browser runner: only `var/control/artifacts` read/write.
- ETL: only `instance/etl` read-only.

See [Architecture](docs/architecture.md) and [Security](docs/security.md) for
the complete trust boundary.

## Quick start

Docker Engine, Docker Compose v2, Python 3, and OpenSSL are required. The test
entry point runs frontend and component checks in Docker; a host Node
installation or project-level npm install is not required.

```sh
./bin/mapp init
./bin/mapp all
```

Initialization creates a private `.env`, initializes control-plane
authentication, and creates the live workspace from the versioned seed when
needed. It must preserve existing `.env`, database, workspace, audit, proposal,
and artifact state.

Local defaults:

- Map: <http://localhost:3000>
- Configuration dashboard: <http://config.localhost:3000>

`./bin/mapp serve` starts the long-running services without rerunning the ETL.
`./bin/mapp etl` refreshes every configured layer, and
`./bin/mapp etl bus_stops` selects one configured layer.

As of 2026-07-16, Leeds still publishes metadata for the configured recent
planning layer but its query operation returns ArcGIS error 400. The loader
fails closed and retains the existing 275-row snapshot. Do not substitute
another Leeds planning layer without reviewing its different meaning, schema,
volume, retention behavior, and workspace mapping. See the
[ETL source-status note](etl/README.md#current-source-status).

For production, do not rely on the local defaults. Read
[Deployment](docs/deployment.md), [Security](docs/security.md), and
[Backup and restore](docs/backup-restore.md) first. The shipped direct
production topology requires distinct public DNS hostnames for map and
configuration traffic and `HTTPS_PORT=443`.

## Configuring the workspace

The dashboard edits the live workspace through server-side validation. It
discovers PostGIS relations visible to the read-only XYZ role, validates
geometry and feature identifiers, checks calculated information expressions,
and runs a bounded render probe before saving.

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
3. Present the explanation, focused diff, warnings, and available baseline
   visual evidence. The current visual test does not render a pending
   candidate in isolation.
4. Apply only after explicit approval.
5. Check XYZ reload status and run a post-apply visual test.

Do not directly edit a remote `workspace.json`. The platform API is the remote
write boundary and records proposal and audit state. The present API still
uses full-access CLI tokens; scoped agent and approval credentials remain a
production requirement documented in [Security](docs/security.md).

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
./bin/mapp etl
./bin/mapp test
./bin/mapp doctor
./bin/mapp verify
./bin/mapp ps
./bin/mapp logs
./bin/mapp db
./bin/mapp reload-xyz
./bin/mapp stop
./bin/mapp down
```

`down` removes containers and networks but is intended to preserve the named
PostgreSQL and Caddy volumes. Always take a backup before database image,
schema, or deployment-boundary changes.

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
values. The last command is an end-to-end runtime check and requires the stack,
database, and representative ETL data.
Dated results and their exact scope are recorded in
[`docs/validation-log.md`](docs/validation-log.md). Treat only the checks
explicitly recorded there as evidence; source restructuring alone is not an
acceptance result.

## Documentation

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
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
