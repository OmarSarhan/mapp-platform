# Operations

## Service lifecycle

```sh
./bin/mapp serve       # start long-running services
./bin/mapp ps          # show service state
./bin/mapp logs        # follow recent service logs
./bin/mapp doctor      # compare .env key names with the example
./bin/mapp test        # run Dockerized component/frontend checks and Compose validation
./bin/mapp stop        # stop without removing containers or data
./bin/mapp down        # remove containers and networks, preserving named volumes
```

Use `./bin/mapp config` after Compose or environment changes. Use
`./bin/mapp verify` for the mode-aware runtime checks. External deployments
also require layer-specific visual acceptance for their own workspace.
If verification finds stale or unresolved container environment values, run
`./bin/mapp up --force-recreate` to replace the runtime containers while
preserving named volumes, then verify again.

Every deployment includes the packaged PostGIS service. `DBS_MAPP` supplies
the database clients' shared connection to it. The semantic service now holds
two credentials of its own: `SEMANTIC_READER_DATABASE_URL` for every catalog
read and `SEMANTIC_DATABASE_URL` for every catalog write. Both reach only the
`semantic` schema of that same database, which is where the catalog lives.

Live deployments keep `MAPP_ENVIRONMENT=production` in `.env`. Caddy is their
only published application endpoint: TCP 80 is redirect/ACME traffic and
TCP/UDP 443 carries the map, dashboard, API, and remote CLI traffic over HTTPS.
Do not publish XYZ or `config-ui` directly. Conflicting exported topology modes
are rejected; use `MAPP_ENV_FILE` when deliberately selecting another complete
environment.

## Loading source data

```sh
./bin/mapp census-check TS001
./bin/mapp demo
```

There is no packaged ETL. The platform database holds derived layers, the
federation registry, the control plane and the semantic catalogue; spatial
data lives in source databases it federates.

`census-check` validates the pinned Nomis archive hashes and performs a
complete ordered scan of all 178,605 Output Area features against the pinned
geometry hash. It reads the publisher over the network and writes to no
database; omit the topic to check all 47 reviewed England topic tables.

`./bin/mapp demo` loads the Leeds showcase into its two source databases
directly from their publishers -- the sample ArcGIS feeds into `ops-db` and
the reviewed England Census 2021 topics into `census-db` -- then registers,
observes and provisions both as federated sources. The source dataset choices
and field meanings are unchanged; the versioned derived catalogue and
published workspace are the parts reconciled by the new demo contract.
Retained source volumes also have the demo-owned read-only login password
reconciled from `.env`; rotating that credential therefore does not require
discarding or reloading either source database.

Before changing federation or derived state, the command requires the live
workspace database, default view, and extent to match the saved demo. Generic
derived planning resolves its authoritative scope from that live workspace, so
continuing across a mismatch could build truncated relations and then publish
the saved extent around them. Restore the packaged scope before rerunning; the
command does not silently choose one side.

The command generically plans all four versioned derived fixtures and checks
all four names before applying the first definition change. It creates missing
definitions, atomically replaces only an exact or recognised older demo
definition, and refuses a same-name definition whose query and interface do
not prove demo ownership. Matching views are retained. The matching
materialized H3 fixture is refreshed after the source reload; a newly created
or replaced materialized fixture is already built from that current source.
Mutations use the bounded background queue one at a time. If every admission
slot is occupied, the command waits and resubmits only the API's proven
non-mutating capacity rejection; after admission it follows the operation to a
terminal result. Every resulting semantic profile must reach `ready` before
workspace publication begins.

For any long-running derived create, replace, or refresh, inspect its retained
ID with `config-cli operations show ID` or continue following it with
`config-cli operations wait ID --progress`. A nonterminal status response can
show the safe database subphase, current activity or wait condition, elapsed
statement time, blocker count, and real PostgreSQL index counters when that
command supports them. `active` means the backend is executing, not that a
generic query exposes measurable row progress; do not cancel or resubmit work
merely because its elapsed time increased. `blocked` identifies lock waiting,
while `unavailable` means only that the separate one-second monitoring sample
could not be obtained. The mutation and its 30-minute PostgreSQL statement
timeout continue independently of that advisory sample.

The final publication replaces the live workspace with the complete saved
ten-layer workspace, including its tile-retry plugin configuration. It is not
a merge with local layers or styles. The command submits the replacement
through the normal revision-bound proposal check, create, approved apply, and
exact-fingerprint XYZ reload path, then reads the workspace back. Proposal
application records its recoverable `applying` state before commit; if the
client loses the response, reconcile the proposal, workspace fingerprint, and
XYZ status rather than assuming the replacement failed. A rerun is idempotent
when the saved workspace and definitions already match.

The full demo workspace is versioned at
`docker/demo-sources/workspace-demo.json`. It remains separate from
`instance/workspace.seed.json`, which must be valid against a fresh packaged
database before any of those four derived relations exists.

Before running `./bin/mapp demo`, confirm headroom on the census-db volume. The raw
statistic values alone are approximately 636 MiB (667 MB), while PostgreSQL
staging, the old and new snapshots, indexes, geometry, and WAL can coexist. Use
6 GiB free only as a minimum planning floor, provision additional operational
headroom, and monitor the database volume during the run. Container `/tmp`
capacity does not replace database-volume capacity.

MAPP idempotently prepares native, EPSG:4326, EPSG:3857, and safe
geometry/geography cross-cast GiST indexes on the relations it owns, and runs
`ANALYZE`. Include this index set in capacity estimates. An existing packaged
volume receives the complete idempotent role, H3, and spatial-index upgrade
automatically before `up`, `serve`, `config-ui` or `all` starts
application/database work. The automatic index ensure analyzes a
relation only when it creates a missing index. `./bin/mapp upgrade-derived`
remains the explicit maintenance entry point, and `./bin/mapp verify` audits
the result without changing database state. Verification also checks the
platform layer-dependency guard table, sync function, public execute grant, and
drop-blocking event trigger used to protect actively referenced workspace and
derived-layer relations from manual database drops.

The final Census publication is atomic and preserves the stable table OID, but
it uses `TRUNCATE` and a complete replacement insert while holding an
`AccessExclusive` lock. Map, semantic, and derived-layer reads can block until
that transaction commits; schedule a refresh window and monitor reader latency.
Before exposing the boundary table through any workspace layer, resolve the
authoritative year in the required OS copyright statement and configure the
complete ONS/OS `layer.attribution`. The `[year]` placeholder is a
display-and-redistribution release gate, not a value to infer.

To rebuild the packaged database from its initialization scripts and leave only
the configured ETL datasets, use the explicitly destructive command:

```sh
./bin/mapp reset-data --confirm
```

It stops the stack, removes only the named bundled PostgreSQL volume, replaces
the live and preview workspaces with `instance/workspace.seed.json`, starts a
fresh database, and runs the unrestricted ETL. This clears workspace layers
which depended on deleted derived or custom relations. Dashboard
authentication, audit records, proposals, artifacts, and public assets live
under `var` and are not reset.

The restored file is intentionally the small base workspace, not the saved
ten-layer demo. After reset has verified that fresh state, `./bin/mapp demo`
reconciles the four managed definitions and proposal-applies the full saved
workspace. Do not copy the demo workspace over the seed: verification runs
before the derived relations are rebuilt.

The semantic catalog no longer survives a reset. It is a schema in the bundled
database, so removing that volume destroys generated profiles, curated
annotations, semantic proposals, history, and archive tombstones with it. The
archival choreography described below still runs, but on the successful path
its result is deleted with everything else; it now protects only the aborted
path, where the volume still exists and the catalog must be left consistent
with a database that was never removed. Take a `pg_dump` first if the curated
metadata matters.

Before deleting the database volume, the command starts only the bundled
database and private control-plane services and installs a durable maintenance
gate with a unique reset owner under the derived-layer advisory lock. The gate
rejects new creates, replacements, refreshes, drops, and confirmed
administrator delivery retries, including already-accepted background jobs
that reach the database after the gate. Automatic outbox delivery continues.
Synchronous API requests blocked by this gate return
`409 derived_layer.maintenance`; an already-accepted background operation
finishes as failed.

Reset performs two bounded delivery checks under that gate:

1. Preflight drains retained work and waits until every current profile is
   `ready` with no undelivered outbox event.
2. Only then does reset queue one archive per current asset and wait until
   every tombstone is confirmed and the outbox is again completely delivered.

Delivery is ordered per asset and managed derived name, so an older pending,
retrying, or `repair_required` event also blocks a replacement asset for that
name. A `repair_required` result or either bounded timeout aborts before volume
deletion. A preflight failure does not queue the archive batch. Correct the
underlying delivery failure and use the confirmed administrator retry if
needed, then run the confirmed reset again. The retry requeues the same event
and payload; it cannot resolve a deterministic conflict by itself.

The gate remains active until volume deletion or until an owner-pinned
completion check confirms that compensation successors are `ready` with no
retained blocker for those names. Concurrent resets are rejected, and ownership
fencing prevents a rejected reset from compensating another reset's gate. On a
handled failure or signal after archival begins but before database volume
removal, the owning exit handler restarts the private control plane, rebinds
each definition left in reset archival state to a new semantic asset ID at
generation 1, and waits for that completion check. Each recovery registration
identifies its archived predecessor. After validating the same derived name and
binding, the semantic service carries forward curated metadata, retained
orphans, visibility, and matching field IDs into the successor and records the
predecessor link in history. Accepted old tombstones remain immutable and are
never unarchived or reused.

Configuration-service startup does not force reset recovery. If a process or
host interruption prevents owned compensation, keep the database volume and
first confirm that no `reset-data` process remains. Then run:

```sh
./bin/mapp recover-reset-data --confirm
```

This explicitly force-recovers the retained gate; never run it against an
active reset. If a reset archive or recovery registration reaches
`repair_required`, correct its underlying cause and rerun the confirmed recovery
command. While the gate remains active, this owner-pinned path is the only retry
allowed; it requeues the exact retained event and payload. If the database
volume was already removed, do not attempt semantic compensation; run the
confirmed reset again to finish initialization. External-database mode rejects
both reset and reset-recovery commands.

The ETL loads the demo's source databases, not the packaged one, and is not a
continuously running or required platform service. If the demo sources need
regular refreshes, schedule `./bin/mapp demo` with the host's approved
scheduler. Prevent overlapping invocations at the
scheduler level even though each target also uses a PostgreSQL advisory lock.

`./bin/mapp demo` loads all three configured sample sources into `ops-db`. A
known ArcGIS rejection is logged concisely and still returns non-zero;
unexpected errors retain their traceback. In either case the failed run is recorded and
deletion reconciliation is skipped. See the
[ETL polygon-source note](../etl/README.md#polygon-source-selection) for the
reviewed replacement of the former planning sample.

Review ETL exit status and the `leeds._etl_runs` and `leeds._etl_layers`
records. Source-count drift, duplicates, conversion errors, or incomplete
fetches intentionally prevent deletion reconciliation.

## Workspace changes

Interactive administrators may use the dashboard. Remote operators and agents
must use the configuration API through the standalone CLI rather than editing
the server filesystem.

For an agent-driven change:

1. Inspect identity, contract, revision, layer, schema, rules, and catalog.
2. Create a revision-bound proposal.
3. Review the explanation, focused diff, warnings, and visual evidence. Use
   top-level visual commands for a live baseline and, when advertised by the
   server contract, proposal preview commands for an isolated rendering bound
   to the stored pending candidate.
4. Apply only after explicit approval. The apply call automatically requests
   and waits for an XYZ reload of the exact committed workspace fingerprint.
5. Check the returned XYZ reload status and run a visual test on the changed
   layer.

If the live revision changed, discard and recreate the proposal. Do not
silently rebase it.

The top-level `locale` is the default even when named alternatives exist.
Named locales are composed with XYZ's own merge semantics. Inspect the
effective layer, but make focused changes to the raw default or named override
that owns the requested value. If raw `/locale` is absent, XYZ uses an empty
synthetic default and does not auto-select a named alternative. Effective named
locales are read-only in the dashboard, including layer add/remove and Advanced
JSON, because editing the composed result could flatten inherited content. Use
a revision-bound `config-cli` proposal targeting the raw
`/locales/<name>/...` path.

## Semantic catalog

The dashboard's **Semantic catalog** and the standalone CLI use the same
authenticated API. Generated profiles are lifecycle records and read-only;
curated metadata follows a separate check, create, explicit approval, and
apply workflow bound to the asset's current version.

The catalog is stored in the packaged database, in a schema named `semantic`,
and is reached only by the private semantic service. Two login roles separate
the two directions of that access:

| Key | Default role | Authority |
| --- | --- | --- |
| `SEMANTIC_DB_USER` | `mapp_semantic` | Owns schema `semantic` and is the only identity that may write it |
| `SEMANTIC_READER_DB_USER` | `mapp_semantic_reader` | `USAGE` on that schema and `SELECT` on its tables, nothing else |

`SEMANTIC_DATABASE_URL` and `SEMANTIC_READER_DATABASE_URL` are the connection
strings built from them. The API and CLI semantic scopes still decide who may
read or change the catalog; the roles are a structural backstop behind that
decision, not a replacement for it. Neither role can reach `derived_layers`,
`federation`, `public`, or any `source_<alias>` schema, and neither holds
`postgres_fdw` `USAGE` or any role membership.

Packaged PostgreSQL services explicitly configure 100 total connections, three
superuser-reserved connections, and no additional reserved connections, leaving
97 ordinary slots. The bundled login-role maxima total 70 and must remain at or
below that usable capacity. The runtime reader's 50-session maximum covers the
two 20-client XYZ pools, the configuration service's process-wide eight-session
`DBS_*` admission gate, and two probe/diagnostic sessions. Packaged source
readers use a 64-session maximum for that runtime budget plus bounded derived,
federation, and semantic-context work and three spare source sessions.

Both roles and the schema are created by the packaged database's initialization
scripts, which run only on an empty data directory. An installation predating
this change needs `./bin/mapp doctor --add-missing` for the six new keys and a
fresh database volume; adding keys to `.env` does not create roles in a volume
that already exists.

Ordinary allowlisted PostgreSQL sources are discovered and synchronized with
`semantic:inspect + semantic:source`. The source action reads relation/column
catalog metadata and standard comments only, under the configured `DBS_*`
read-only role and an `ACCESS SHARE` lock; it never reads rows, values,
defaults, or expressions. An unchanged metadata digest is a no-op. Review
`SEMANTIC_SOURCE_ALLOWLIST` and the database role whenever an expected source
is missing; an explicitly empty allowlist disables this facility.

`SEMANTIC_SOURCE_EXCLUSIONS` subtracts exact selectors from that allowlist. A
new exclusion stops future discovery and synchronization but does not rewrite
the existing semantic store. To remove profiles registered before the setting
changed, first retain their asset IDs for audit, then use the dashboard's
selected-profile archive action individually, or use
`config-cli semantic source archive-excluded --confirm` (the bulk API action)
with `semantic:inspect + semantic:admin`. Neither operation drops or changes
the PostgreSQL relation. Archived assets disappear from catalog, search, and
derived-profile collections for all callers; exact asset/history lookups
remain available only to administrators by a retained ID. Removing an
exclusion later does not unarchive the tombstone.

Creating a managed derived relation commits its generated semantic-profile
event in the same PostgreSQL transaction. The caller's `derive` scope is
sufficient for this automatic record. Delivery continues after the request:
the configuration service wakes its worker after a derived write, drains
outstanding events in the background, and resumes the drain at startup.
Workers atomically claim eligible events in PostgreSQL with an expiring lease;
unexpired claims exclude other workers, while expiry releases abandoned work.
A repeated dispatch across lease expiry remains safe because event processing
is idempotent.

Review these public states on the derived definition:

- `registering` should normally converge to `ready` without operator action;
- `ready` means its current generation is in the semantic catalog; and
- `repair_required` means a retained event is blocking later generations; an
  administrator must resolve the cause and explicitly retry that same event.

Administrators can inspect a derived profile's name-level delivery diagnostic
in the dashboard or API. It reports the blocking operation, generation, state,
attempt count, event ID, and bounded single-line error without exposing the
retained payload or worker claim. A narrow token needs both
`semantic:inspect` and `semantic:admin`. The response's top-level
`catalogRevision` is authoritative from the live semantic service; if that
service is unavailable, the read fails with `503` rather than reporting a
locally inferred revision.

For a failed archive after its derived definition was dropped, the
administrator list exposes a separate `deliveryBlockers` entry. It can be
retried by the retained name from the dashboard or CLI and reports
`pending_archive` while queued.

Do not edit `derived_layers._semantic_outbox` or the `semantic` schema
manually. They are now schemas in one database, but they remain separate stores
written by separate roles in separate transactions. A retained event has a
stable ID and payload hash, so retry is idempotent; hand-editing either schema
can break that evidence. The route named `repair` only requeues the retained event and cannot
correct a deterministic 4xx, corrupt event, or invalid acknowledgement. Check
`semantic-service` and `config-ui` logs without copying the internal token,
database URLs, or curated data into an issue.

Registration availability gates only a newly published derived reference.
An existing reference can remain unchanged with a warning, and it can be
removed. When a new workspace proposal is blocked, wait for `ready` or have a
semantic administrator resolve the cause and retry the profile event; do not
bypass validation by editing the workspace file.

For curated changes, review the check result before creating a proposal.
Proposal creation is not approval to apply. In the dashboard, **Apply** stays
disabled until the exact stored pending proposal has been fetched and its
explanation and focused diff rendered for review; the list summary is not
sufficient evidence. Applying or declining records the decision actor and
time separately from the proposal creator. If a source event or another
proposal changes the asset version, inspect again and create a new proposal
rather than rebasing the old one.

Use a focused semantic `unset` proposal when only an annotation should be
removed. Unsetting `/curated/description`, one property below
`/curated/fields/<field-id>`, or the complete field annotation keeps the
generated table/column profile and database data in place. Do not archive the
asset merely to clear reviewed wording, and do not try to edit generated facts
through a curated proposal.

The selected asset also exposes retained orphaned annotations and lazily loaded
immutable history. History is served through the authenticated gateway, and
its entries and catalog revision are read from the same semantic-store
snapshot.

See [Semantic metadata control plane](semantic-layer.md) for the complete
scope and status model.

## Reloads

Every successful dashboard workspace save and applied CLI proposal requests a
reload generation. The caller waits for the supervisor to report the exact
saved workspace fingerprint as healthy. The dashboard displays the restart
while the save request is in flight and reports when connection readiness is
confirmed. The XYZ supervisor restarts only its child application process and
records the applied generation, workspace fingerprint, start time, and health
in `var/reload`.

Use `./bin/mapp reload-xyz` only for an intentional local operator-requested
reload. It fingerprints the current workspace, requests a generation, and
waits for the supervisor to report TCP readiness with that fingerprint. This
is evidence that the expected workspace file was loaded, not proof of HTTP,
database-backed rendering, or cartographic quality.

From a separate operator computer, the corresponding confirmed command is
`config-cli reload-xyz --confirm` (also available as
`config-cli xyz reload --confirm`). Both spellings call the authenticated
`POST /api/xyz/reload` endpoint, which derives the current live workspace
fingerprint and waits for the same readiness condition. These are
recovery/operator actions, not an extra step after a normal save or proposal
apply.

Do not edit `var/workspace/workspace.json` directly. Filesystem changes are not
watched because they would bypass validation, revision checks, proposals, and
audit records.

A save or proposal apply can commit before reload confirmation times out. An
HTTP 504 carrying `saved: true`, or an `applied` proposal with an applied
revision, is not an ordinary failed write. Reconcile the workspace revision,
proposal status, fingerprint, and XYZ status before considering a retry.

## Visual tests

The configuration service chooses a view from the layer's geometry extent and
map scale, then delegates to the internal browser runner. Reports and
screenshots are stored in `var/control/artifacts` and served only through
authenticated API routes.

The live visual endpoints remain useful for baseline and post-application
verification. Proposal visual endpoints render the stored pending candidate
through the dedicated `xyz-preview` process. Candidate publication and browser
completion are serialized, and the response and artifacts are bound to the
proposal ID and candidate hash. Preview state lives in `var/preview` and
`var/preview-reload`; it never replaces `var/workspace` or requests a live XYZ
reload.

Use the proposal `screenshot` endpoint for approval evidence. It defaults to a
square 1080×1080 viewport at 1× device scale and viewport-only capture, producing
an exact 1080×1080 page image. The response records actual PNG dimensions under
`visual.capture`; authenticated artifact routes remain the only way to read the
images. Width, height, and device scale are bounded to prevent unbounded browser
resource use. The isolated renderer publishes the proposal's retained original
first and its candidate second at the same view, so `before*` and `after*`
artifacts show the configuration change rather than pre-click/post-click
candidate states. For feature-information diffs, both renders select the same
representative feature and wait for the expanded left information panel; the
response also includes panel-only before/after artifacts.

When the selected layer has active hover configuration, the runner deliberately
moves the mouse onto the planned representative feature and captures the
visible XYZ infotip. Set `hover: true` to require this check or `hover: false`
to suppress it; optional `expectedHoverText` assertions are matched inside the
infotip only. Treat hover as verified only when `visual.hover.passed` is true
and `artifacts.hoverTooltip` (or the proposal comparison's corresponding
before/after hover artifact) is non-null.

Live visual plans/tests and proposal previews also accept bounded
`expectedInfoPanelText`. The runner opens clicked-feature information at the
planned interaction, matches those strings only inside the expanded panel,
and records whether the click was requested, attempted, opened, and captured,
plus a specific failure reason, identity checks, the match map, panel sample,
and dedicated `infoPanel` artifact. Treat the content as evidence only when the
identity-bound interaction passed and that artifact is present; a generic
page-text change is insufficient.

For add, remove, or move changes involving a grouped layer, the comparison
switches on only the changed layer: additions appear only after, removals only
before, and moves remain isolated on both sides. Other folder members stay off
while remaining available in navigation. Ordinary style or configuration edits
that keep the same membership retain the active group for context. A removed
layer's candidate image intentionally contains no proposal layer.

When browser validation does not pass, the API returns HTTP 422 while
preserving the plan, failed report, and authenticated artifact paths. Review
those artifacts as failure evidence.

Background visual operations also publish a current `stage` and refreshed
`updated` timestamp. A terminal transition also records `finished`; active
operations are never removed when bounded terminal history is pruned. Poll
until a terminal status; a browser or end-to-end
deadline returns `failed` with `failedStage` and bounded console, page-error,
and request-failure diagnostics. Timeouts cover launch, page readiness,
capture, artifact persistence, and the final atomic operation write. The
browser slot is released after bounded cleanup, so a later request can run
without restarting a healthy XYZ service.

The browser runner permits one active test by default. `MAX_CONCURRENT_RUNS`
is hard-clamped to the range 1–4, and the runner rejects excess internal
requests with HTTP 429 and a short retry hint rather than launching additional
Chromium processes. The configuration API propagates that 429 with the selected
plan. Queue or retry visual work conservatively; this concurrency bound does
not provide artifact retention or a total-storage quota.

`VISUAL_RUN_TIMEOUT_MS` defaults to 90000 and bounds one browser run end to
end. The configuration service uses `VISUAL_BROWSER_TIMEOUT_SECONDS` (default
90) for its runner request and `VISUAL_BACKGROUND_TIMEOUT_SECONDS` (default
300) for the durable operation, allowing a comparison screenshot to render
both retained sides while still guaranteeing a terminal status.

Large or outlier-heavy data, external basemaps, unusual zoom rules, themes, and
custom SVGs may need manual review. External framework and basemap assets use
the dedicated Squid proxy; the browser runner itself remains on an internal
network. `instance/browser-egress-allowlist.txt` contains reviewed Squid
`dstdomain` entries and defaults to OpenStreetMap's tile domain. Add a hostname
only in the same review as the workspace asset that needs it. The proxy allows
only HTTPS `CONNECT` tunnels to port 443. It rejects unlisted hostnames without
DNS lookup, then rejects reviewed names that resolve to private or reserved
addresses, and does not log request URLs.

Run the deterministic behavior check after changing the policy:

```sh
docker compose --env-file .env.example -f compose.yaml build egress-proxy
python scripts/test_egress_proxy.py
```

The check uses an isolated local TLS origin and requires an allowlisted HTTPS
hostname to return 200 while an unlisted hostname and plaintext HTTP return
403. It removes its uniquely
named test containers and network when complete.
Use the standalone CLI's bounded `--lng`, `--lat`, and `--zoom` options
together when the automatic extent is not representative. A complete explicit
view is validated before database planning and skips the relation-wide count,
extent, and representative-feature queries; the browser instead exercises the
map centre. If automatic planning fails first, use the returned
`planningStage` and `queryPurpose` to distinguish the feature-count/extent
stage from representative-feature selection. No browser artifacts exist for a
pre-browser planning failure.
Visual tests need retention, quota, and storage monitoring before long-term
production use.

`./bin/mapp cleanup-temp` performs a dry run of the deliberately narrow
seven-day cleanup policy. It lists completed browser artifact run directories
and abandoned atomic-write temporary files, but preserves the live workspace,
authentication, audit history, proposals, the semantic database and history,
and reload state. It only ever inspects `var`; the semantic catalog is a
schema in the packaged database and is out of its reach entirely. Review the
JSON candidate list, then run
`./bin/mapp cleanup-temp --confirm` to remove exactly those disposable paths.
Unrecognized directories and trees containing symlinks or special files are
left untouched for manual review.

## Logs and audit

- Container logs are operational diagnostics and may include database or
  source errors. Restrict access and avoid pasting them into public issues.
- `var/control/audit.jsonl` records authentication and configuration events.
- `var/control/proposals` retains complete original and candidate workspaces.
- `var/control/artifacts` may contain map data visible in screenshots.

Back up and protect these records. A rotation, retention, archival, and disk
monitoring policy remains to be implemented.

## Credentials

### Configuration dashboard password

To replace a known or forgotten administrator password, run this from the
platform checkout on the deployment host:

```sh
./bin/mapp reset-config-password
```

The command generates a new password and prints it once. Store it immediately
in the approved password manager, then sign in to the dashboard again. The
reset invalidates every existing dashboard session and takes effect without a
service restart. It does not change or revoke remote CLI bearer tokens.

`./bin/mapp init --demo` is a separate disposable-environment transition. On
an existing instance it validates the demo settings first, then rotates the
administrator password, clears dashboard sessions, and revokes every CLI API
token and outstanding device authorization before enabling the demo sources.

The wrapper uses the deployment's selected `.env`. If the deployment
deliberately uses another environment file, select the same file used to run
the stack:

```sh
MAPP_ENV_FILE=/secure/path/production.env ./bin/mapp reset-config-password
```

To revoke all remote CLI tokens as a separate incident-response action, run:

```sh
./bin/mapp revoke-config-tokens
```

The dashboard's **Access and audit** panel provisions semantic reader,
proposal author, AI semantic author, curator, delivery operator, semantic
administrator, or full platform operator tokens. Expand **Customize
narrow scopes** to choose an exact workspace-and-semantic combination. Presets
do not broaden the resulting credential: the server stores and enforces only
the submitted scopes. Existing token rows expose their exact stored grants
under **Granted permissions**. **Pending device authorizations** appears only
while at least one unapproved request is actually pending.

These commands affect configuration-service authentication only. They do not
change PostgreSQL passwords. Changing `.env` passwords also does not rotate
roles in an existing PostgreSQL volume; perform database password rotation
explicitly and update dependent services together. That now includes
`SEMANTIC_DB_PASSWORD` and `SEMANTIC_READER_DB_PASSWORD`: change the role in
the database and the matching connection string in `.env` in one maintenance
window, then recreate `semantic-service` so both connections are re-established.

XYZ and configuration discovery intentionally share the exact `DBS_MAPP`
connection. For an external database, rotate that URI through the approved
secret process and recreate both services together. Database administration,
backups, and role changes remain the external operator's responsibility.

## Routine checks

- Confirm all services are healthy.
- With a local database, confirm recent ETL runs succeeded and expected row
  counts, including Census when loaded, are plausible.
- Confirm the semantic service responds, the catalog revision is readable, and
  managed profiles are not unexpectedly stuck in `registering` or
  `repair_required`.
- Check free space for bundled PostgreSQL when used, Caddy, proposals, audit,
  semantic history, and artifacts; monitor external PostGIS through its
  operator.
- Review failed logins, token usage, and unexpected proposal activity.
- Test backups and restores on a separate environment.
- Verify TLS renewal and production cookie settings.
- Periodically rebuild and scan images, then deploy accepted digests.
