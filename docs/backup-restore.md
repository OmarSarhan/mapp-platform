# Backup and restore

A recoverable deployment needs both database and filesystem state. Container
images and the versioned `instance` directory are not sufficient.

## What to back up

| Data | Location | Reason |
| --- | --- | --- |
| PostgreSQL database | Named database volume, preferably logical dump | Map data, ETL control records, schema |
| Live workspace | `var/workspace` | Current configuration and previous atomic save |
| Control state | `var/control` | Authentication hashes, token records, audit, proposals, artifacts |
| Reload state | `var/reload` | Useful for consistent recovery diagnostics; can be regenerated cautiously |
| Deployment secrets | `.env` and external secret-store records | Database and service credentials |
| Versioned inputs | Git repository and accepted release tag | Seed, ETL manifest, public assets, image definitions |
| Caddy state | Named Caddy volumes | Certificates and Caddy runtime data |

Store backups outside the deployment host, encrypt sensitive material, restrict
access, and record checksums and retention dates.

## Database backup

Before an image, schema, state-boundary, or PostgreSQL change:

```sh
mkdir -p backups
docker compose --env-file .env exec -T db \
  pg_dump -U postgres -d mapp -Fc > backups/mapp.dump
```

Use the actual configured database and administrator names when they differ.
Protect the dump as sensitive data.

A filesystem copy of a running PostgreSQL volume is not automatically
consistent. Prefer `pg_dump`, a database-aware snapshot, or a documented
physical-backup process.

## State backup

For the strongest consistency, pause configuration writes while copying
`var/workspace` and `var/control`. Preserve file modes and ownership. Proposal
records contain complete original and candidate workspaces; audit records and
screenshots may also be sensitive.

Back up `.env` separately through an encrypted secret-management process. Do
not place it in the same unencrypted archive as public release files.

## Restore order

1. Provision a clean host with the intended platform release.
2. Restore `.env` from the approved secret store without committing it.
3. Restore the PostgreSQL dump into a fresh, compatible database volume.
4. Restore `var/workspace` and `var/control` with the configured host UID/GID
   and restrictive modes.
5. Restore Caddy data if retaining the existing certificate state is
   appropriate, or allow Caddy to obtain new certificates.
6. Initialize or clear stale reload coordination deliberately, then start the
   stack.
7. Verify database health, current workspace revision, authentication, audit
   readability, XYZ reload fingerprint, public map rendering, and a visual
   test.

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
- archive checksums;
- restore-test date and result;
- the operator who performed the test.

Retention automation and periodic restore testing remain production-readiness
items.
