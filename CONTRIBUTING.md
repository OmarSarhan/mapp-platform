# Contributing

## Before starting

Discuss substantial architecture, API, database, security, or deployment
changes with the owner before implementation. The platform and standalone CLI
are separate repositories; place changes in the component that owns them.

No project-level licence has been selected. Do not assume that the repository
or bundled assets may be redistributed, and do not add third-party code or
assets without documented provenance and compatible permission. See
[`LICENSING.md`](LICENSING.md).

## Development principles

- Keep the GEOLYTIX XYZ framework unmodified and pinned to a verified upstream
  commit.
- Preserve unknown workspace extension properties.
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

Select checks appropriate to the change:

```sh
./bin/mapp test
./bin/mapp doctor
./bin/mapp config
./bin/mapp verify
```

Changes to initialization or paths should also be tested from a clean state and
an existing populated state. Changes to backup or database lifecycle require a
restore test. API changes require standalone CLI compatibility tests.

## Documentation and changelog

Update the focused document under `docs` and add a concise entry to
[`CHANGELOG.md`](CHANGELOG.md) for user-visible behavior. Do not turn the
validation log into a claim about a check that was not run.
