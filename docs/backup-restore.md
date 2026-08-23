# Backup and restore

A recoverable deployment needs both database and filesystem state. Container
images and the versioned `instance` directory are not sufficient.

## What to back up

| Data | Location | Reason |
| --- | --- | --- |
| PostgreSQL database | Bundled named volume or the external operator's backup system | Map data, spatial indexes, and schema; sample ETL control records in bundled mode |
| Live workspace | `var/workspace` | Current configuration and previous atomic save |
| Control state | `var/control` | Authentication and device state, token records, audit, proposals, durable operations, artifacts |
| Semantic state | `var/semantic` | Generated and curated profiles, proposals, event receipts, history, and archive tombstones |
| Reload state | `var/reload` | Useful for consistent recovery diagnostics; can be regenerated cautiously |
| Preview scratch state | `var/preview`, `var/preview-reload` | Ephemeral proposal rendering state; recreate it from the restored live workspace rather than treating it as authoritative |
| Deployment secrets | `.env` and external secret-store records | Database and service credentials |
| Versioned inputs | Git repository and accepted release tag | Seed, ETL manifest, public assets, image definitions |
| Caddy state | Named Caddy volumes | Certificates and Caddy runtime data |

Store backups outside the deployment host, encrypt sensitive material, restrict
access, and record checksums and retention dates.

## Database backup

The command below applies to the local-database modes,
`MAPP_DATABASE_MODE=bundled` and `federated`:

Before an image, schema, state-boundary, or PostgreSQL change:

```sh
umask 077
install -d -m 700 backups
docker compose --env-file .env -f compose.yaml \
  -f compose.bundled-db.yaml exec -T db \
  pg_dump -U postgres -d mapp -Fc > backups/mapp.dump
```

Use the actual configured database and administrator names when they differ.
Protect the dump as sensitive data.

For `MAPP_DATABASE_MODE=external`, do not use the platform's local `db`
command or volume. Back up and restore the database through the external
service's approved tooling, retention policy, and recovery process. Coordinate
database and platform-state recovery points so the restored workspace still
matches the restored schemas and relations.

A filesystem copy of a running PostgreSQL volume is not automatically
consistent. Prefer `pg_dump`, a database-aware snapshot, or a documented
physical-backup process.

## State backup

For the strongest consistency, pause configuration and derived-layer writes
while copying `var/workspace`, `var/control`, and `var/semantic`. Stop
`config-ui` first so it cannot commit another PostgreSQL outbox event, then
stop `semantic-service` before copying its directory. The semantic database
uses SQLite write-ahead logging; copy the complete directory while the service
is stopped rather than copying only `semantic.sqlite3` from a running service.

Take the PostgreSQL dump or coordinated external snapshot during the same
write-quiesced interval. The derived definition and semantic outbox live in
PostgreSQL, while delivered profiles and event receipts live in SQLite. A
coordinated recovery point lets pending events replay idempotently after
restore; unrelated snapshots can instead claim that an event was delivered
when the restored semantic catalog does not contain it.

Preserve file modes and ownership. Proposal records contain complete original
and candidate workspaces; device authorization, durable operation, audit,
semantic annotations/history, and screenshot records may also be sensitive.
The preview workspace and preview reload channel are scratch state, not
required backup inputs.

Back up `.env` separately through an encrypted secret-management process. Do
not place it in the same unencrypted archive as public release files.

## Restore order

1. Provision a clean host with the intended platform release.
2. Restore `.env` from the approved secret store without committing it.
3. Restore the PostgreSQL dump into a fresh compatible bundled volume, or have
   the external operator restore the target PostGIS database and connection.
4. Restore `var/workspace`, `var/control`, and the matching `var/semantic`
   snapshot, including durable operation records, with the configured host
   UID/GID and restrictive modes. Do not restore stale `var/preview` scratch
   state; leave it absent so initialization seeds it from the restored live
   workspace.
5. Restore Caddy data if retaining the existing certificate state is
   appropriate, or allow Caddy to obtain new certificates.
6. Initialize or clear stale live and preview reload coordination
   deliberately, then start the stack. Startup resumes ordinary pending and
   retrying outbox delivery, but deliberately does not force recovery of a
   retained reset maintenance gate.
7. If the restored PostgreSQL state contains a gate from an interrupted reset,
   confirm that no `reset-data` process exists in the restored environment,
   then run `./bin/mapp recover-reset-data --confirm`. The command assigns new
   semantic asset IDs at generation 1 to definitions left in reset archival
   state. Each registration names its validated archived predecessor so
   curated metadata, orphans, visibility, and matching field IDs carry into
   the audited successor without unarchiving or reusing the old tombstone.
8. Allow the outbox to deliver every retained event before evaluating profile
   readiness. Do not clear a restored worker claim manually; its bounded lease
   expires and makes abandoned work eligible again. A restored
   `repair_required` event does not self-requeue; correct its cause and use the
   confirmed administrator retry, which sends the same retained payload.
9. Verify database health, current workspace revision, authentication, audit
   readability, semantic schema/catalog revision, derived-profile readiness,
   XYZ reload fingerprint, public map rendering, and a visual test.

Do not overwrite a healthy production deployment while testing a restore. Use
an isolated host, DNS name, network, and database.

## PostgreSQL upgrades

Patch-image refreshes should still be preceded by a backup and restore test. A
major upgrade cannot be performed by changing the image tag against the old
volume. Use a tested `pg_upgrade` or logical dump/restore plan and account for
the target image's volume layout.

## Recovery objectives and testing

The owner must define acceptable recovery point and recovery time objectives.
Until automated schedules exist, record:

- backup time and platform release;
- database and PostGIS versions;
- workspace revision and fingerprint;
- semantic schema version and catalog revision;
- archive checksums;
- restore-test date and result;
- the operator who performed the test.

Retention automation and periodic restore testing remain production-readiness
items.

The [production acceptance evidence workflow](production-acceptance.md)
provides explicit backup and isolated-restore hook points. A hook pass records
that the reviewed executable succeeded; retain its protected operator log and
backup checksums because hook output is deliberately excluded from the
redacted JSON report.
