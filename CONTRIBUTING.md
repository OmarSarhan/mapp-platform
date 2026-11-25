# Contributing

## Before starting

Discuss substantial architecture, API, database, security, or deployment
changes with the owner before implementation. The platform and standalone CLI
are separate repositories; place changes in the component that owns them.

Original source, documentation, configuration, schemas, and repository-authored
sample assets in this repository are licensed under the MIT License
([`LICENSE`](LICENSE), selected on 2026-08-04; see [`LICENSING.md`](LICENSING.md)
and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)). Contributions are
accepted under the same licence unless stated otherwise. Do not add third-party
code or assets without documented provenance and compatible permission.

## Development principles

- Keep the GEOLYTIX XYZ framework unmodified and pinned to a verified upstream
  commit.
- Preserve supported schema-declared map entries and validated plugin
  properties, and reject unsupported keys outside the audited pinned-XYZ
  workspace contract rather than silently dropping them (see
  [`AGENTS.md`](AGENTS.md), `config-ui/workspace_schema.py`, and
  `config-ui/schema/workspace.schema.json`).
- Keep versioned `instance` inputs separate from ignored live `var` state.
- Maintain least-privilege database roles, networks, containers, and mounts.
- Do not bundle the standalone CLI into this repository or an application
  image.
- Treat API behavior as a cross-repository compatibility contract.
- Never include secrets or sensitive runtime artifacts in commits, fixtures,
  logs, screenshots, or review descriptions.

## Changes

Use focused commits and include:

- the problem and intended behavior;
- security, migration, and compatibility effects;
- documentation updates;
- tests run and exact results;
- known checks that could not be run.

Do not rewrite unrelated user changes. Runtime data under `var` belongs to the
operator and must not be deleted or regenerated casually.

## Validation

Select checks appropriate to the change, keeping static checks distinct from
container/database-mutating runs:

- **Static / read-only checks** (no database or container mutations):
  `./bin/mapp doctor` (compares `.env` key names against `.env.example`) and
  `./bin/mapp config` (renders/validates Compose configuration).
- **Full test suite — scratch DB + build/container mutations (dedicated dev checkout only):**
  `./bin/mapp test` uses the active checkout's `ROOT_DIR`, `.env`, and
  `compose.bundled-db.yaml` overlay; it builds `config-ui`, `semantic-service`,
  `mcp-auth`, and `mapp-leeds-etl:local`, runs `docker compose up --detach --build --wait db`
  on the selected deployment's database server, drops and recreates
  `mapp_control_test` and `mapp_semantic_test` on that server, and runs
  `npm ci`/build/audit plus container test suites. Run `./bin/mapp test`
  **only** from a dedicated development checkout with its own `.env`, unique
  Compose project/volume identities, and non-production data — never treat it
  as an automatically isolated production check.
- **Live stack verification:** `./bin/mapp verify` inspects a reviewed running
  deployment (including optional MCP services only when `MAPP_MCP=1` is set).

```sh
./bin/mapp doctor
./bin/mapp config
./bin/mapp test    # dedicated dev checkout only (builds/starts db, drops/recreates scratch DBs)
./bin/mapp verify  # against a reviewed running stack
```

Changes to initialization or paths should also be tested from a clean state and
an existing populated state. Changes to backup or database lifecycle require a
restore test. API changes require standalone CLI compatibility tests.

## Documentation and changelog

Update the focused document under `docs` and add a concise entry to
[`CHANGELOG.md`](CHANGELOG.md) for user-visible behavior. Do not turn the
validation log into a claim about a check that was not run.
