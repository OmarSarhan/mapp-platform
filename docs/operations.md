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

`MAPP_DATABASE_MODE=bundled` includes the local PostGIS service. With
`MAPP_DATABASE_MODE=external`, `serve` starts only XYZ, the configuration
service, browser runner, and Caddy; `DBS_MAPP` supplies their shared external
connection. The external database lifecycle is not managed by this wrapper.
Before switching an existing bundled deployment to external mode, take a
backup and run `./bin/mapp down`; otherwise its already-running `db` container
is outside the newly selected service set and remains untouched.

Live deployments keep `MAPP_ENVIRONMENT=production` in `.env`. Caddy is their
only published application endpoint: TCP 80 is redirect/ACME traffic and
TCP/UDP 443 carries the map, dashboard, API, and remote CLI traffic over HTTPS.
Do not publish XYZ or `config-ui` directly. Conflicting exported topology modes
are rejected; use `MAPP_ENV_FILE` when deliberately selecting another complete
environment.

## ETL

```sh
./bin/mapp etl bus_stops
./bin/mapp etl definitive_paths
./bin/mapp etl smoke_control_orders
```

To rebuild the bundled database from its initialization scripts and leave only
the configured ETL datasets, use the explicitly destructive command:

```sh
./bin/mapp reset-data --confirm
```

It stops the stack, removes only the named bundled PostgreSQL volume, replaces
the live and preview workspaces with `instance/workspace.seed.json`, starts a
fresh database, and runs the unrestricted ETL. This clears workspace layers
which depended on deleted derived or custom relations. Dashboard authentication,
audit records, proposals, artifacts, and public assets are not reset. External-
database mode rejects this command.

The ETL is optional sample-data provisioning for the bundled database, not a
continuously running or required platform service. The wrapper disables it in
external mode. If the sample stack needs regular refreshes, schedule it with
the host's approved scheduler. Prevent overlapping invocations at the
scheduler level even though each target layer also uses a PostgreSQL advisory
lock.

The unrestricted `./bin/mapp etl` loads all three configured sources. A known
ArcGIS rejection is logged concisely and still returns non-zero; unexpected
errors retain their traceback. In either case the failed run is recorded and
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

For add, remove, or move changes involving a grouped layer, the comparison
switches on only the changed layer: additions appear only after, removals only
before, and moves remain isolated on both sides. Other folder members stay off
while remaining available in navigation. Ordinary style or configuration edits
that keep the same membership retain the active group for context. A removed
layer's candidate image intentionally contains no proposal layer.

When browser validation does not pass, the API returns HTTP 422 while
preserving the plan, failed report, and authenticated artifact paths. Review
those artifacts as failure evidence.

The browser runner permits one active test by default. `MAX_CONCURRENT_RUNS`
is hard-clamped to the range 1–4, and the runner rejects excess internal
requests with HTTP 429 and a short retry hint rather than launching additional
Chromium processes. The configuration API propagates that 429 with the selected
plan. Queue or retry visual work conservatively; this concurrency bound does
not provide artifact retention or a total-storage quota.

Large or outlier-heavy data, external basemaps, unusual zoom rules, themes, and
custom SVGs may need manual review. External framework and basemap assets also
require outbound DNS and HTTPS from the runner's dedicated egress network.
Use the standalone CLI's bounded `--lng`, `--lat`, and `--zoom` options when
the automatic extent is not representative.
Visual tests need retention, quota, and preferably an egress allowlist or
local asset mirror before long-term production use.

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

These commands affect configuration-service authentication only. They do not
change PostgreSQL passwords. Changing `.env` passwords also does not rotate
roles in an existing PostgreSQL volume; perform database password rotation
explicitly and update dependent services together.

XYZ and configuration discovery intentionally share the exact `DBS_MAPP`
connection. For an external database, rotate that URI through the approved
secret process and recreate both services together. Database administration,
backups, and role changes remain the external operator's responsibility.

## Routine checks

- Confirm all services are healthy.
- In bundled mode, confirm recent sample ETL runs succeeded and expected row
  counts are plausible.
- Check free space for bundled PostgreSQL when used, Caddy, proposals, audit,
  and artifacts; monitor external PostGIS through its operator.
- Review failed logins, token usage, and unexpected proposal activity.
- Test backups and restores on a separate environment.
- Verify TLS renewal and production cookie settings.
- Periodically rebuild and scan images, then deploy accepted digests.
