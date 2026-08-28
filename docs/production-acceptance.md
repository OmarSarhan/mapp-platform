# Production acceptance evidence

Production acceptance is an observed release exercise, not a checklist that can
be completed from repository contents. The evidence command records what it
could verify and leaves unobservable controls `pending` with a reason:

```sh
./bin/mapp production-acceptance
```

The default report is
`var/acceptance/production-evidence.json`. It is created atomically with mode
`0600` under a mode-`0700` directory and must not be committed. The report
contains check identifiers, statuses, concise results, and safe reasons. It
does not copy environment values or hook output.

The basic run validates:

- private environment-file permissions and the production-only settings;
- selection of the production topology;
- the resolved production Compose model when Docker is available;
- whether live service, DNS, TLS, HTTP, and host checks still require a
  production-host run;
- whether backup, isolated restore, upgrade, and rollback rehearsals still
  require explicit hooks.

A failed check exits `1`. Pending checks are evidence gaps rather than
successes. They exit `0` by default so a preparation run can write its report;
use `--require-complete` to exit `3` when any check is pending.

## Live production-host run

After the reviewed release is running, execute:

```sh
./bin/mapp production-acceptance --live --require-complete
```

The live run queries both public DNS names, verifies their system-trusted TLS
chains and hostnames, requests the public endpoints, reads Compose health, and
attempts to read a supported host firewall (`ufw`, `firewall-cmd`, or `nft`).
It never changes DNS, certificates, firewall rules, containers, or data.

A readable host firewall is evidence of observability, not evidence that its
policy is correct. Review the captured host policy separately and record the
provider load-balancer, security-group, or network-firewall review in the
release record. Those external controls cannot be inferred by this process.
Confirm that only the intended SSH administration path and public TCP 80,
TCP 443, and UDP 443 are reachable; PostgreSQL and application container ports
must remain unpublished.

Successful TLS inspection proves that a currently valid public chain and
hostname were presented. It does not prove future ACME renewal. Exercise
renewal against staging or monitor a real automatic renewal, then retain the
Caddy event and certificate-expiry evidence with the release record.

## Backup, isolated restore, upgrade, and rollback

Rehearsals are deployment-specific and potentially destructive. The harness
will not guess their targets or run arbitrary commands from `.env`. Supply
reviewed, executable, argument-free hook files explicitly:

```sh
./bin/mapp production-acceptance \
  --live \
  --run-rehearsals \
  --backup-hook /approved/hooks/create-backup \
  --restore-hook /approved/hooks/restore-isolated \
  --upgrade-hook /approved/hooks/upgrade-isolated \
  --rollback-hook /approved/hooks/rollback-isolated \
  --require-complete
```

Each hook must:

- fail closed with a non-zero exit status;
- operate against an explicitly isolated host, project name, DNS namespace,
  database, volumes, and `var` copy;
- enforce its own target guard before changing state;
- avoid printing secrets; stdout and stderr are suppressed from the evidence
  report and represented only by a SHA-256 digest;
- clean up only resources it created;
- perform the assertions below rather than merely completing a command.

The backup hook must create a fresh database-aware backup plus protected,
coordinated copies of `var/workspace`, `var/control`, Caddy state, and the
release identity; the semantic catalog is inside the database backup rather
than beside it. It must verify archive readability, private
permissions, checksums, and the off-host destination.

The restore hook must restore that exact backup into new storage and verify
database/PostGIS health, representative data, workspace revision and
fingerprint, authentication state, audit readability, semantic catalog
revision and derived-profile readiness, proposal/artifact access, XYZ reload
status, and representative visual behavior. It must never restore over the
live deployment.

The upgrade hook must start the candidate release against the isolated restored
state, record accepted image digests, and run `./bin/mapp verify` plus the
release's configuration and visual checks.

The rollback hook must restore the previous accepted application images
against the isolated state, then repeat health, configuration, reload, and
visual checks. If an upgrade changes a database schema or PostgreSQL major
version, the hook must exercise the separately reviewed data rollback or
restore plan; container recreation alone is not a rollback.

Passing hook evidence means the exact executable returned success during this
run. Preserve the reviewed hook source, its output in an access-controlled
operator log, the report, backup checksums, release/image digests, timestamps,
operator identity, and measured recovery time outside the deployment host.

## Acceptance record

Do not promote a release until the evidence has no failures and every pending
item has either been directly rerun successfully or is accompanied by a named,
dated external review. Record:

- release/tag and immutable image digests;
- production and restored workspace revisions/fingerprints;
- production and restored semantic catalog revisions;
- database and PostGIS versions;
- backup identifiers, checksums, recovery point, and retention destination;
- restore start/end times and measured recovery time;
- DNS answers, certificate expiry and renewal evidence;
- host and provider firewall review;
- upgrade and rollback rehearsal identifiers;
- service, reload, API, and representative visual results;
- operator/reviewer identities and unresolved exceptions.

Never edit the JSON to turn a pending or failed observation into a pass. Rerun
the check or attach the separate, attributable external evidence.
