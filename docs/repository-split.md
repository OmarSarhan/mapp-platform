# Repository split

## Current staging layout

The parent workspace currently contains two repository-ready directories:

- `mapp-platform` — deployable server, database, dashboard/API, browser
  validation, ETL, instance inputs, and operational documentation.
- `mapp-config-cli` — standalone remote client and agent guidance.

The parent directory has no `.git` metadata. The directory move is therefore a
content split only; it is not evidence of a history-preserving migration.

## Required history-preserving migration

Perform the authoritative split from a fresh clone of the canonical monorepo:

1. Freeze writes and tag the last accepted monorepo revision.
2. Mirror or clone all branches and tags into protected working copies.
3. Run `git filter-repo --analyze` and inventory large files, generated
   artifacts, credentials, and path history.
4. Create the platform history by retaining the platform files and removing
   the old bundled CLI implementation, wrapper, CLI-only documentation, and
   CLI-specific agent guide.
5. Create the CLI history by retaining the old CLI implementation, wrapper,
   CLI documentation, and relevant shared commits.
6. Apply the staged directory and packaging changes as normal commits after
   filtering. Do not use this history-less workspace as the source repository.
7. Preserve meaningful annotated tags or create documented replacement tags.
   Record how old monorepo tags map to each new repository.
8. Verify authorship, timestamps, renames, release notes, and blame for sample
   files in both outputs.
9. Run a full-history secret scan on the monorepo and both filtered histories.
10. Rotate any credential that may have appeared in history or a shared
    artifact; history rewriting does not revoke a secret.
11. Run repository-specific tests and cross-repository API compatibility
    checks.
12. Publish the two repositories, then archive the monorepo read-only with
    links and migration notes.

`git filter-repo` removes remotes by design and can rewrite object IDs. Review
its documentation, work on disposable clones, and retain an untouched mirror
until both new repositories are accepted.

## Platform ownership after the split

The platform repository owns:

- Compose topology and service images.
- The configuration dashboard and server-side API.
- The server-authoritative workspace schema, validation rules, examples, and
  contract version.
- ETL code and versioned instance manifest.
- Live-state initialization and mount boundaries.
- Deployment, operations, backup, security, and incident documentation.

It does not own or bundle the Python CLI client.

## CLI ownership after the split

The CLI repository owns:

- Client packaging, command parsing, profiles, credential-file handling, and
  HTTP transport.
- Agent workflow guidance.
- Compatibility tests against supported platform contract versions.
- Client releases and installation documentation.

It must not copy server validation logic and claim it is authoritative. The
platform's `/api/contract`, `/api/schema`, `/api/rules`, and validation
responses remain the source of truth.

## Cross-repository release contract

Each platform release should publish its API, contract, rules, schema, and XYZ
versions. Each CLI release should document the platform contract versions it
supports and fail clearly on an incompatible major version.

At minimum, a compatibility gate should exercise:

- public identity and authenticated contract discovery;
- workspace read and revision reporting;
- schema, rules, catalog, icons, and examples;
- dry-run mutation and validation errors;
- proposal create/show/list/decline/apply and stale-revision conflict;
- reload status and visual testing;
- authentication failure and redaction behavior.

The two repositories may release independently once that contract and CI gate
exist.
