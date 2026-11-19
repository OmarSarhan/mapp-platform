# Backup and restore

A recoverable deployment needs both database and filesystem state. Container
images and the versioned `instance` directory are not sufficient.

## What to back up

| Data | Location | Reason |
| --- | --- | --- |
| PostgreSQL database | Bundled named volume or the external operator's backup system | Map data, spatial indexes, and schema; sample ETL control records with a local database; the `semantic` catalog schema holding generated and curated profiles, proposals, event receipts, history, and archive tombstones; the `control` schema holding the administrator credential, dashboard sessions, CLI token records, device authorizations and the MCP authorization component's OAuth records |
| Live workspace | `var/workspace` | Current configuration and previous atomic save |
| Control file state | `var/control` | Audit log, proposals, durable operations, artifacts. Authentication, session, token and device-authorization records are **not** here any more; they are in the database's `control` schema, and the audit log is the deliberate exception that stayed a file |
| Reload state | `var/reload` | Useful for consistent recovery diagnostics; can be regenerated cautiously |
| Preview scratch state | `var/preview`, `var/preview-reload` | Ephemeral proposal rendering state; recreate it from the restored live workspace rather than treating it as authoritative |
| Deployment secrets | `.env` and external secret-store records | Database and service credentials |
| Versioned inputs | Git repository and accepted release tag | Seed, ETL manifest, public assets, image definitions |
| Caddy state | Named Caddy volumes | Certificates and Caddy runtime data |

Store backups outside the deployment host, encrypt sensitive material, restrict
access, and record checksums and retention dates.

## Database backup

The command below backs up the packaged database:

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

Federated source databases are backed up by whoever operates them, through
their own approved tooling, retention policy, and recovery process. Coordinate
those recovery points with this one so the restored workspace still matches the
restored schemas and relations.

A filesystem copy of a running PostgreSQL volume is not automatically
consistent. Prefer `pg_dump`, a database-aware snapshot, or a documented
physical-backup process.

## State backup

Fence public traffic and private clients. Drain configuration, browser and
operator jobs, then stop config-ui, the renderers, browser runner and optional
MCP services. Keep the database running for the dump. Take the live workspace,
control files and referenced artifacts while those writers remain stopped.
Record the release commit, image digests, PostgreSQL and extension versions,
control migration ledger, `control.metadata` instance ID, current recovery
epoch and semantic catalog revision in the protected backup manifest.

Record the workspace byte hash and nanosecond modification time. The workspace
revision includes both. For example, from the deployment root on Linux:

```sh
umask 077
sha256sum var/workspace/workspace.json backups/mapp.dump
python3 -c 'import os; print(os.stat("var/workspace/workspace.json").st_mtime_ns)'
tar --format=pax --numeric-owner -cpf backups/state.tar var/workspace var/control
sha256sum backups/state.tar
```

Take the PostgreSQL dump or coordinated external snapshot during the same
write-quiesced interval. That dump is now also the only copy of the
authorization state: an operator who restores `var/control` without a matching
database restore recovers the audit trail and proposals but no credential to
sign in with. `var/mcp-auth` holds only a socket and is not a backup input.
The derived definition, the semantic outbox, and the
delivered profiles and event receipts now all live in that one database, so a
single dump is internally consistent across the bridge and the old failure
mode is gone: a snapshot can no longer claim an event was delivered while a
separately copied semantic catalog does not contain it. What still has to be
quiesced is the filesystem state beside the database, because the workspace and
control records must come from the same interval as the dump.

Preserve file modes and ownership. Proposal records contain complete original
and candidate workspaces; device authorization, durable operation, audit,
semantic annotations/history, and screenshot records may also be sensitive.
The preview workspace and preview reload channel are scratch state, not
required backup inputs.

Back up `.env` separately through an encrypted secret-management process. Do
not place it in the same unencrypted archive as public release files.

## Restore order

1. Provision a clean, isolated destination with the backup release and compatible
   PostgreSQL major version, extensions, roles and grants. Keep all ingress and
   other database clients fenced through the private readiness checks. Stop
   automated restarts. Preserve the original backup for a fresh attempt.
2. Restore `.env` from the approved secret store without committing it.
3. Verify the archive checksums. For the bundled logical restore, initialize
   the fresh volume's required roles using a separate bootstrap database name.
   Leave the database named in the archive absent. With just the destination
   database service running, restore from the maintenance database:

   ```sh
   pg_restore --exit-on-error --create -d postgres mapp.dump
   ```

   This restores the archived database name, settings and database-level
   privileges, including the semantic roles' CONNECT grants. After successful
   restore, set the deployment's database name back to the name in the manifest.
   A failed create-and-restore may leave a partial database. Keep the target
   fenced and retry on fresh storage. Check role settings, schema and object
   ownership, FDW mappings, extensions and database privileges for every
   service role. The dump relies on the destination's cluster roles.
4. Restore `var/workspace` and `var/control`, including durable operation
   records, with the configured host UID/GID and restrictive modes. The
   semantic catalog and the control schema — the administrator credential,
   sessions, CLI tokens, device authorizations and OAuth records — need no
   separate step; they returned with the database in step 3. That is also why
   step 5 exists: they returned *as they were*, including credentials revoked
   since the snapshot was taken. Do not recreate
   `var/mcp-auth` by hand; the wrapper creates the directory and the
   authorization component creates the socket. Do not restore stale
   `var/preview` scratch state; leave it absent so initialization seeds it from
   the restored live workspace.
5. Before invoking the wrapper, compare the restored bytes, file modes,
   ownership and workspace mtime with the manifest. Verify proposal, operation
   and artifact references. Resolve mismatched files from a matching capture.
   A rounded timestamp changes the revision even if the bytes match. Recreate
   and review affected proposals before applying them.

   Inspect the restored control ledger without starting application services:

   ```sh
   ./bin/mapp migrate-restored-control
   ./bin/mapp migrate-restored-control --expect-instance "$BACKUP_INSTANCE_ID" --confirm
   ```

   The first command prints the observed identity, ledger and pending migrations
   and exits 2 for review. Compare the identity and ledger to the manifest.
   The confirmed command accepts initialized, contiguous ledgers known to this
   release and runs the normal forward migrations in one transaction. Migration
   11 also revokes legacy approval windows. Missing identity, ownership or ledger
   evidence needs operator investigation. An unknown newer ledger requires the
   matching release. Keep all credential issuers and consumers stopped.

   Review the epoch and invalidate restored authority at a separate checkpoint:

   ```bash
   ./bin/mapp advance-recovery-epoch --expect-instance "$BACKUP_INSTANCE_ID"
   ./bin/mapp advance-recovery-epoch --confirm --expect-instance "$BACKUP_INSTANCE_ID"
   ```

   A snapshot contains every credential that was valid when it was taken,
   **including ones revoked since**, so restoring it hands them back. This
   advances the recovery epoch, which revokes dashboard sessions, CLI API
   tokens, device authorizations, and MCP grants and their tokens. The
   administrator credential, registered OAuth clients and the audit log are
   untouched, and nothing here is unrecoverable — it returns by signing in and
   consenting again.

   Do this **before** starting the stack. The command needs only the database
   and deliberately does not start the application, because a pre-restore
   credential is usable until this has run. Run it without `--confirm` first to
   see what it would invalidate.

   The sweep requires this release's complete control ledger and sweep tables.
   A failed transaction leaves the old epoch and revocations intact. If the
   command loses its response or file audit fails after commit, keep traffic
   fenced. Inspect the database epoch against the captured value, revocation
   state and audit before deciding whether to retry. A second confirmed sweep
   advances the epoch again. Recheck the restored administrator credential and
   client registrations against subsequent password rotations or client
   withdrawals before permitting sign-in or fresh consent.
6. Restore Caddy data if retaining the existing certificate state is
   appropriate, or allow Caddy to obtain new certificates.
7. Initialize or clear stale live and preview reload coordination
   deliberately, then start the stack privately. Startup resumes ordinary pending and
   retrying outbox delivery, but deliberately does not force recovery of a
   retained reset maintenance gate.
8. If the restored PostgreSQL state contains a gate from an interrupted reset,
   confirm that no `reset-system` process exists in the restored environment,
   then run `./bin/mapp recover-reset-system --confirm`. The command assigns new
   semantic asset IDs at generation 1 to definitions left in reset archival
   state. Each registration names its validated archived predecessor so
   curated metadata, orphans, visibility, and matching field IDs carry into
   the audited successor without unarchiving or reusing the old tombstone.
9. Allow the outbox to deliver every retained event before evaluating profile
   readiness. Do not clear a restored worker claim manually; its bounded lease
   expires and makes abandoned work eligible again. A restored
   `repair_required` event does not self-requeue; correct its cause and use the
   confirmed administrator retry, which sends the same retained payload.
10. Verify old sessions and tokens are refused, then verify a fresh login,
    scoped issuance and any enabled MCP consent. Check the control and semantic
    ledgers, audit, restored source identity and grants, pending operation
    outcomes, profile generations and catalog revision after replay. Request
    a fresh XYZ reload, match the fingerprint to the restored workspace, then
    render a representative layer, select a known feature and inspect visual
    evidence over the private route. Record the outcome and any explicitly
    accepted degraded coverage before opening traffic.

Do not overwrite a healthy production deployment while testing a restore. Use
an isolated host, DNS name, network, and database.

## Rehearsed recovery boundary

The November 2026 rehearsal used a fresh bundled volume, PostgreSQL 17.10,
PostGIS 3.5.7, H3 4.2.3 and control ledger 11. A separate read-only PostgreSQL
source supplied one synthetic polygon. Before capture, the dashboard published
the source and a managed view, replaced that view, and separately saved the
suggested workspace edit. Both sides of the restore ran in a private network
namespace.

The drill restored a custom-format dump with `pg_restore --create` and the
matching PAX file archive, checked the manifest identity, ran
`migrate-restored-control` and advanced the epoch. Four CLI tokens were
revoked and two sessions removed. The API refused the old bearer and cookie
and accepted fresh login and scoped token issuance. The workspace revision
and a fresh XYZ reload fingerprint matched the manifest. The same derived
asset was ready at generation 2. Browser validation activated the layer,
opened feature 7 and retained the map and information-panel captures.

In a negative rehearsal, restoring into a differently named empty database
without its database-level ACLs left the semantic service without CONNECT.
The destination stayed fenced. A separate PostgreSQL 18 control-only drill
dumped and restored fixtures for every ledger from 1 through 11, checked a
rounded workspace timestamp, and verified that migration preserved identity
and the subsequent sweep invalidated restored credentials. Those fixtures
exercise control-ledger compatibility. Qualify other complete release and
PostgreSQL-major combinations separately.

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

The [production acceptance evidence workflow](deployment.md#production-acceptance-evidence)
provides explicit backup and isolated-restore hook points. A hook pass records
that the reviewed executable succeeded; retain its protected operator log and
backup checksums because hook output is deliberately excluded from the
redacted JSON report.
