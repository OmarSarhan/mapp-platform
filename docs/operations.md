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
`./bin/mapp verify` for the complete runtime acceptance check.

## ETL

```sh
./bin/mapp etl
./bin/mapp etl bus_stops
```

The ETL is a one-shot tool, not a continuously running service. Schedule it
with the host's approved scheduler if regular refreshes are required. Prevent
overlapping invocations at the scheduler level even though each target layer
also uses a PostgreSQL advisory lock.

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
3. Review the explanation, focused diff, warnings, and baseline visual
   evidence. Explicitly disclose that the pending candidate is not rendered
   by the current visual test.
4. Apply only after explicit approval.
5. Check XYZ reload status and run a visual test on the changed layer.

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

An applied proposal requests a reload generation. The XYZ supervisor restarts
only its child application process and records the applied generation,
workspace fingerprint, start time, and health in `var/reload`.

Use the local `reload-xyz` command only for an intentional operator-requested
reload. A healthy reload status is evidence that the expected workspace was
loaded; it is not proof of cartographic quality.

A save or proposal apply can commit before reload confirmation times out. An
HTTP 504 carrying `saved: true`, or an `applied` proposal with an applied
revision, is not an ordinary failed write. Reconcile the workspace revision,
proposal status, fingerprint, and XYZ status before considering a retry.

## Visual tests

The configuration service chooses a view from the layer's geometry extent and
map scale, then delegates to the internal browser runner. Reports and
screenshots are stored in `var/control/artifacts` and served only through
authenticated API routes.

The current runner renders the live workspace. It is useful before a change as
baseline evidence and after application as verification, but it is not a
candidate preview.

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

Use dashboard administration to rotate the administrator password and revoke
remote tokens. Changing `.env` passwords does not rotate roles in an existing
PostgreSQL volume; perform database password rotation explicitly and update
dependent services together.

## Routine checks

- Confirm all services are healthy.
- Confirm recent ETL runs succeeded and expected row counts are plausible.
- Check free space for PostgreSQL, Caddy, proposals, audit, and artifacts.
- Review failed logins, token usage, and unexpected proposal activity.
- Test backups and restores on a separate environment.
- Verify TLS renewal and production cookie settings.
- Periodically rebuild and scan images, then deploy accepted digests.
