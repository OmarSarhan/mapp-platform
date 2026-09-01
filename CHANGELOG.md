# Changelog

This project has not yet established a release cadence or semantic-versioning
policy. Entries are collected under **Unreleased** until the owner defines the
first release.

## Unreleased

### Removed

- Removed `MAPP_DATABASE_MODE`. MAPP packages its own PostgreSQL, which holds
  the derived layers, the federation registry and the control plane, so there
  is no longer a deployment axis to name: the database is always present and
  spatial source data always lives in separate databases reached over
  `postgres_fdw`. The `bundled`, `federated` and `external` values, the
  predicates that read them, and the subcommand guards that refused under
  `external` are all deleted. This is a breaking change for any env file that
  sets the key.
- Removed `compose.federation-external.yaml` and its documentation. It let an
  external host database act as the federation host; with the packaged
  database always filling that role there is nothing for it to do.

### Added

- Federation groups, as labels. A group is a name, an optional description and
  who created it; a source belongs to zero or more. Membership grants nothing,
  revokes nothing, and changes no PostgreSQL privilege -- it records which
  sources are meant to be used together. `config-cli federation groups`,
  `group-define`, `group-delete` and `set-groups` manage them, and the
  federation panel shows each source's labels. Labels rather than enforcement
  because every provisioned source is already a foreign table in one packaged
  database, so cross-database joins work between any two of them regardless;
  grouping could only restrict that, and a source in two groups bridges them
  anyway. Reading needs `federation:observe`; defining, deleting and assigning
  need `federation:register`. Existing databases gain the column by migration
  on the next start.

- The semantic catalogue moved out of the SQLite file at `var/semantic` and
  into the packaged database, in a schema named `semantic`. Two new login
  roles carry it: `SEMANTIC_DB_USER` (default `mapp_semantic`) owns the schema
  and performs every write, and `SEMANTIC_READER_DB_USER` (default
  `mapp_semantic_reader`) holds `USAGE` and `SELECT` and nothing else, which
  every read goes through. The API and CLI semantic scopes remain the primary
  gate; the roles are the structural backstop behind them. Six new environment
  keys -- the two users, their passwords, and the two DSNs -- so an existing
  install needs `./bin/mapp doctor --add-missing` and a fresh database volume,
  because the roles and schema are created by the init script that only runs
  on an empty one. Catalogue contents are not migrated from the old SQLite
  file.

- Added `MAPP_DEMO_SOURCES` and `./bin/mapp init --demo`, which turn the
  two-source Leeds showcase on for a deployment. Every wrapper command then
  applies `compose.federated-demo.yaml` and starts `census-db` and `ops-db`
  alongside the platform, and `./bin/mapp demo` seeds both source databases and
  publishes the map layers built from them. Existing installs will see
  `./bin/mapp doctor` report `MAPP_DEMO_SOURCES` as missing until
  `./bin/mapp doctor --add-missing` adds it; the added value is empty, which is
  the non-demo setting.
- `./bin/mapp test` now stands up a scratch database for the semantic-service
  suite. Those tests need a real server, and without one every test in the
  suite skipped while the run still reported success.
- `./bin/mapp demo` now generates every semantic draft with the bounded data
  context -- sample rows and column statistics -- rather than from column
  names alone. The dashboard offers these per generation and defaults them
  off, because sending rows to a model is a judgement about the data in front
  of you; the demo makes that judgement once, on published government open
  data.
- `./bin/mapp demo` now describes the exposed relations and their fields with
  Gemini, so the catalogue reads as prose rather than as a list of column
  names. It is on whenever `GEMINI_APIKEY` is set; `./bin/mapp demo
  --no-semantics` turns it off, and an absent key skips the step rather than
  failing the build. Descriptions are drafted and applied without review, which
  each proposal's recorded explanation says. Each relation gets its table and
  first `MAPP_DEMO_FIELD_LIMIT` fields described (50 by default in `.env`),
  while remaining fields stay at the structural profile --
  `census_2021_england_oa` has 470 columns, and normally one model call per
  field is not what a demo is for. The cap is announced in the output rather
  than applied silently. The semantic stage reports the exact planned target
  count and advances a terminal-safe progress bar as generation targets settle.

### Fixed

- Derived background work now has a bounded two-job FIFO by default: one job
  executes while a second waits without holding a database connection. Jobs
  publish durable worker/database/result stages, queued cancellation never
  touches PostgreSQL, and a database-independent endpoint exposes sanitized
  active operation IDs and queue positions for CLI inspection. Raising the
  admission limit no longer launches competing mutations that immediately
  fail on the database advisory lock. A failed worker-stage write now records
  a terminal preflight failure instead of leaving an orphaned running record,
  and capacity rejections keep rejection-time counts separate from their fresh
  queue snapshot.
- Repeated demo runs now preserve and skip already curated table and field
  annotations instead of spending Gemini quota to regenerate them. A Gemini
  429 now waits and retries after 5, 15 and 45 seconds; a limit during parallel
  work uses one recovery probe and makes the remaining requests serial. If the
  bounded retries are exhausted, the optional phase falls back to structural
  profiles, explicitly discards results from the unfinished relation, and gives
  source-specific capacity or quota guidance rather than failing the demo or
  draining every queued request.
- Census demo loading now limits expensive invalid-geometry accounting and
  repair to the active workspace extent when that extent is valid, and records
  the selected bounds in dataset provenance. Geometries outside those bounds
  are loaded unchanged and do not count against the repair ceiling; an absent
  or invalid workspace extent retains the full-dataset behaviour.
- Re-enabling demo mode now revokes every existing CLI API token and
  outstanding device authorization together with the demo-only administrator
  credential rotation. The access dashboard also exposes each token's stored
  permissions in a read-only expandable checklist and hides **Pending device
  authorizations** when no request is pending. After a one-time API token is
  confirmed on the clipboard, its copy button now flashes mint and reads
  **Copied** instead of leaving the action visually unacknowledged.
- Concurrent XYZ, configuration discovery, validation, planning, statistics,
  and semantic-context work can no longer overrun a shared PostgreSQL reader.
  Every configured `DBS_*` read in the configuration service now passes through
  one process-wide eight-session admission gate, while optional semantic
  context retains its tighter three-read bound. The runtime role now reserves
  50 sessions for two upstream 20-client XYZ pools, eight admitted configuration
  reads, and two probes; source readers reserve 64 for all bounded consumers.
  Fresh and retained bundled/demo databases apply those limits, packaged
  clusters explicitly provide 97 ordinary slots from `100 - 3 - 0`, and
  verification rejects stale active capacity settings. The dashboard still
  schedules context-enabled field requests three at a time and metadata-only
  requests ten at a time while preserving field order and all-or-nothing
  review.
- New workspaces enable a source-controlled locale-wide tile recovery plugin.
  MVT render and feature sources plus raster sources retry transient transport,
  `408`, `429`, and selected `5xx` failures at most twice with capped backoff,
  jitter, and `Retry-After`; deterministic visible `4xx` responses remain
  terminal. Existing workspaces can enable the same policy through the normal
  locale proposal and preview flow without changing individual layers.

- Requesting sample rows or column statistics for a **federated** relation
  always failed with `semantic.generation_context_unavailable`, so the
  dashboard's data-context option looked unsupported for federated sources
  rather than broken. The read issued `LOCK TABLE`, which PostgreSQL refuses
  on a foreign table. The profiling path already excluded foreign tables from
  that lock; this call site never got the same guard.

- `docker/demo-sources/layers.sh` profiled the exposed relations before
  provisioning them. Profiling reads the `source_<alias>` foreign tables that
  provisioning creates, so the step could never have succeeded on a first run.
  It failed silently because every call used `curl -sS` without `--fail` and
  piped the response into a helper that printed the error code and exited 0 --
  so a demo build could report complete success with no showcase present. The
  calls now fail their pipeline, and rebuilding acknowledges the physical
  rebind that re-seeding legitimately causes.
- `./bin/mapp reset-data` could be blocked permanently by a derived layer whose
  semantic profile sat in `repair_required`. Nothing could move that status --
  the repair route works from an outbox event, and a profile can be left in
  this state with no event -- so the preflight waited out its budget and
  failed, every run. The preflight now queues an archive for such a profile
  rather than waiting for it, which is what clears it: an asset the catalogue
  no longer holds answers 404, and that is recorded as delivered.
- `./bin/mapp verify` read `leeds.bus_stops`, `leeds.definitive_paths`,
  `leeds.smoke_control_orders` and the whole Census manifest from the packaged
  database through the runtime reader and the derived owner. Those relations
  moved to the source databases, so the checks could not pass on a database
  that never had the schema -- which is what `reset-data` leaves behind. They
  are deleted rather than retargeted; the Census assertions already run against
  `census-db`.
- `docker/demo-sources/seed.sh` ran `psql` against the source databases with no
  readiness wait, while those containers install openssl and generate a
  certificate before PostgreSQL starts.
- `./bin/mapp` and `./bin/mapp verify` now apply `compose.federation-test.yaml`
  when its connection reference is configured, so recreating `config-ui` no
  longer strips `FEDERATION_DBS_LEEDS_EXT` and leaves aliases using it
  unverifiable.
- `./bin/mapp verify` now reports an absent `federation` registry schema as its
  own failure, naming the `CREATE SCHEMA` statement that fixes it, rather than
  aborting the audit with `invalid_schema_name`.

- Added federated PostgreSQL source retirement, which archives the schema,
  server and foreign tables under a name no alias can occupy rather than
  dropping them, drops only the user mappings because those are live
  credentials rather than audit evidence, and refuses while a derived layer,
  a workspace layer or an unreachable semantic service still depends on the
  source.
- Added continuous verification of provisioned federated sources on a
  fifteen-minute cadence, with a bounded pass that takes the
  least-recently-verified alias first. A source that becomes unreachable, or
  whose connection configuration becomes unusable, loses consumer access
  automatically and regains it on the first pass that succeeds. Startup waits
  briefly for the first pass rather than blocking on an unreachable third
  party.
- Added `sourceState` to semantic assets, so a profile whose federated source
  is retired or unreachable is flagged rather than deleted and stops
  authorising derived planning, while keeping its identity and curated meaning
  so a source that returns reclaims exactly its own annotations. Semantic
  service contract `1.2.0`.
- Added `./bin/mapp federation-test`, an end-to-end harness driving the whole
  lifecycle over HTTP against a genuinely separate source database, including
  a cross-schema H3 aggregation checked against the same computation run
  locally, and semantic degradation and recovery.
- Added [Federated PostgreSQL sources](docs/federation.md), the operator
  procedure for attaching, verifying and withdrawing a source, with every
  `federation.*` error code documented. API and contract version `1.6`.

- Added bounded numeric layer statistics with null/non-finite counts, min/max,
  discrete quantiles, overflow-safe histograms, requested-threshold counts,
  and candidate exclusive class counts without returning source rows.
- Added a read-only area-weighted H3 recipe planner that validates one ready
  semantic polygon source, resolves the saved map scope, generates guarded
  overlap-mode candidate SQL, prefilters every supported source SRID by the
  same scope, and returns the exact create request and normal preflight probes
  without mutating database or workspace state.

### Fixed

- Effective visual planning now reports its source/filter provenance and also
  applies configured feature-set/lookup restrictions. Boolean fixed filters
  use valid PostgreSQL literals, structured scalar filters preserve XYZ's
  PostgreSQL coercion semantics, raw percent operators remain executable, and
  unreliable structured forms fail validation. Empty geometries are excluded,
  JSON-compatible identifiers are retained, and an empty result reports its
  exact count, stage, and reason. Bounded category aggregation now uses the
  same restrictions; advanced browser-managed sources still report configured
  restrictions when database probing is intentionally skipped.
- Durable operations now record an explicit terminal timestamp and history
  pruning never removes active work. New noncanonical-but-supported layer keys
  receive an actionable machine-key warning.
- Visual planning now applies a layer's validated `filter.default` to feature
  count, extent, representative-feature selection, and focus bounds. Sparse
  layers therefore focus a feature that XYZ can actually render, while an
  empty effective dataset returns `visual.no_matching_features` before browser
  execution.
- Candidate visual evidence now rejects layer keys that pinned XYZ cannot
  register and records configured candidate keys, resolved URL keys, group
  membership, registered drawers, and actual OpenLayers collection visibility
  in a per-requested-layer verdict. UI wording is informational, structural
  group registration drives the verdict, and unavailable map inspection fails
  closed instead of being inferred from URLs or DOM text.
- Added stage-aware visual worker deadlines from Chromium launch through page
  readiness, screenshot capture, artifact persistence, and durable result
  persistence. Timed-out or crashed runs now retain structured diagnostics,
  leave `running` terminally, release browser capacity, and cannot overwrite a
  watchdog failure with a late result.
- Synchronous visual tests and proposal screenshots now create their durable
  operation before read-only planning and terminalize pre-browser rejection at
  the `planning` stage, so every accepted browser submission remains pollable.
- Documented and validated XYZ's native `groupClassList` styling contract for
  layer-group drawers, including first-member precedence and the requirement
  for a verified deployed stylesheet class rather than a literal colour.
- Derived-layer create and replace now use the selected effective locale's
  configured north/east/south/west extent as the output scope instead of a
  smaller startup-view viewport; older workspaces without all four bounds keep
  the existing view-derived fallback.
- Moved Chromium visual tests and screenshots onto durable background
  operations when requested, so browser completion atomically retains reports,
  artifact references, and explicit failures for operation polling.
- Replaced blocking derived-mutation advisory-lock admission with a bounded
  non-waiting check. Competing mutations and proven-rollback PostgreSQL lock
  timeouts now return a retryable `derived_layer.database_contention` conflict
  with a closed contention scope and actionable operator guidance, while
  uncertain outcomes remain non-retryable and fail closed.
- Completed the existing bundled-database upgrade lifecycle: normal startup
  now applies the idempotent derived-role/H3 upgrade before application
  services and ensures missing managed spatial indexes without repeatedly
  analyzing unchanged relations.
- Prepared EPSG:27700 GiST expression indexes for managed source and
  materialized geometry so metric area/intersection joins can remain indexed
  instead of comparing every projected source and generated row.
- Combined proven literal-generator and scoped H3 row bounds with PostgreSQL
  plan structure so underestimated `ProjectSet`, `Function Scan`, and CTE
  inputs cannot hide over-budget nested-loop pair work. Rejections now carry a
  versioned, closed planning probe and general index-preserving rewrite
  guidance without hard-coding spatial predicates or query templates.
- Compared approved H3 polygon-wrapper `search_path` settings semantically,
  retaining `pg_catalog` precedence and exact authoritative extension-schema
  membership while tolerating harmless quoting, whitespace, and extension
  ordering. H3 capability reporting now verifies the exact extension-owned
  geometry overload and executes a bounded nested-dependency readiness probe.
  Failed readiness now reports a closed, non-sensitive extension, version,
  catalog, policy, planning, execution, or result-validation diagnosis while
  keeping non-H3 derived queries available.
- Reported authoritative derived-mutation failure phases and commit state,
  distinguishing preflight and proven rollback from commit, reporting,
  polling, and recovery uncertainty without exposing database internals.
- Pinned the installed H3 PostGIS polygon SQL wrappers to the catalog-derived
  trusted extension path, allowing their nested `ST_Dump` call to resolve while
  PostgreSQL plans and refreshes materialized views under its restricted search
  path without admitting same-named custom routines.
- Allowed schema-qualified PostGIS/H3 cast types only after pre-analysis proves
  exact allowlisted-extension membership and an authoritative controlled
  `public` namespace match, so geometry typmods can resolve under the restricted
  search path without weakening the existing explicit-typmod and positive-SRID
  output requirement.
- Changed growing semantic, derived-profile, source-relation, and
  workspace-proposal pages to bounded keyset/limit+1 reads with
  integrity-bound revision, configuration, and visibility cursors, plus a
  documented 16 MiB response ceiling below the gateway client limit. Legacy
  parameterless shapes remain compatible through 100 items and now fail with
  `pagination.required` instead of materializing more; the dashboard uses
  explicit, independent Load-more cursors for every semantic collection.
- Bounded administrator derived-profile delivery diagnostics to one blocker
  per displayed profile plus a 100-item unmatched repair batch, with
  `deliveryBlockersMore` making remaining work explicit.
- Forwarded live visual-test clicked-feature text assertions into the browser
  interaction plan so `expectedInfoPanelText` produces dedicated, verifiable
  information-panel evidence instead of being accepted but ignored. Click
  evidence now separately records request, attempt, panel opening, capture,
  identity checks, and a specific failure reason.
- Archived semantic assets are now omitted from catalog, search, and
  derived-profile collections, including administrator collection reads;
  exact administrator lookups retain their immutable audit history.
- Made proposal evidence report friendly layer titles as informational text,
  use exact pinned-XYZ drawer hooks for Filtering/Styling capture, deliberate
  hover-tooltip interaction, and per-side clicked-feature capture for added,
  removed, or edited information content.
- Fixed catalog discovery for PostGIS geometry columns on materialized derived
  layers, allowing dashboard and CLI validation and XYZ map configuration to
  recognize them.

### Changed

- Split managed-derived query failures into malformed, policy-prohibited, and
  over-budget codes with reason-specific remediation, operation-specific
  unchanged-state messages, synchronous/background parity, and explicit
  estimated-versus-rolled-back actual materialization evidence.
- Completed machine-readable action discovery for live visual plan, test, and
  screenshot commands and for durable derived-layer work, including exact
  scopes, operation kinds, request schemas, and presentation metadata.
- Added configuration-driven semantic source exclusions plus confirmed
  administrator actions to archive all existing matches or one selected
  semantic profile without changing PostgreSQL data; normal collections hide
  tombstones while exact-ID administrator history remains auditable.
- Added independently opt-in, permission-gated Gemini context for a capped 5%
  row sample and table/field statistics in Guided, Advanced, API, and CLI
  generation flows; batches of up to ten field drafts now run concurrently
  with completion progress while preserving review order.
- Made the server-resolved fixed workspace-map extent mandatory for managed
  derived layers, with antimeridian-safe output filtering at one zoom level
  wider, whole-query aggregate semantics, and a non-writing 1 GiB plan-size
  guard that blocks oversized materialized creates, conversions, and refreshes
  while offering an ordinary view instead.
- Added a fail-closed computation guard for every managed derived query, with
  structural SQL limits, map-bounded H3 expansion, recursive PostgreSQL plan
  budgets, structured probe evidence, and no ordinary-view escape for queries
  whose intermediate work is unsafe.
- Added PostgreSQL role-level connection, memory, temporary-file, parallelism,
  statement, transaction, lock, and idle-transaction limits for derived work
  and runtime reads, fixed derived-owner namespace resolution to
  `pg_catalog, public`, and bounded background-job admission with a retryable
  structured capacity response.
- Added a manifest-backed external XYZ plugin registry with hashed discovery,
  strict dynamic schemas, proposal and preview bindings, declarative browser
  evidence, dashboard/CLI discovery, and cross-service mount verification.
- Restricted the advertised workspace schema to capabilities verified in the
  pinned XYZ v4.23.4 source, with typed native templates, layer gazetteer,
  dictionaries, SVG templates, roles, and the exact bundled plugin registry;
  properties outside the audited contract are rejected with their exact path
  rather than preserved or silently removed.
- Added atomic, validated derived-layer replacement and kind conversion, with
  structured blocking feedback for PostgreSQL dependents and dashboard
  workspace references.
- Added first-class XYZ layer-folder support through validated per-layer
  `group` values, grouped dashboard navigation, and a layer-folder editor.
- Added first-class controls for XYZ's interactive layer Styling panel,
  including panel visibility and ordered audited `style.elements` keys.
- Added schema validation and dashboard controls for XYZ layer Filtering
  panels, layer-level behavior, and per-information-field filters.
- Made proposal screenshots render a high-resolution, isolated
  original-versus-candidate comparison instead of labelling candidate
  pre-click/post-click images as before/after. Feature-information changes now
  select the same feature on both sides, wait for the expanded information
  panel, and retain full-page and panel-only comparison artifacts.
- Extended proposal screenshots with optional Filtering and Styling panel
  capture, including dedicated before/after panel artifacts and text-presence
  evidence checks.
- Made proposal preview plans and screenshots group-aware: layer additions,
  moves, and removals are isolated from other folder members, while ordinary
  edits that keep membership unchanged retain their group as visual context.
- Replaced the broken Leeds recent-planning sample with the bounded Smoke
  Control Orders polygon layer under a new table and workspace mapping, and
  made expected ArcGIS service failures report concisely without hiding their
  non-zero automation signal or weakening deletion safety. Bundled verification
  now selects a database-backed MVT layer from the current mutable workspace
  instead of assuming that the original Bus Stops seed entry is still present.
- Added machine-readable action discovery, request/operation correlation,
  durable bounded operation evidence, structured visual diagnosis, scoped
  expiring device authorization, and server-side scope enforcement while
  retaining legacy full-token and proposal workflow compatibility.
- Added a redacted production acceptance evidence command covering environment
  and Compose validation, optional live DNS/TLS/HTTP/service/firewall
  observations, and explicit isolated backup/restore/upgrade/rollback rehearsal
  hooks. Unobservable controls remain pending with reasons.
- Made dashboard operations serialize safely, freeze edits while requests are
  active, distinguish unknown save outcomes, and visibly progress saves from
  XYZ restart to fingerprint-matched connection readiness.
- Made local and authenticated standalone operator reloads derive the current
  workspace fingerprint and wait for the matching supervisor generation;
  dashboard saves and CLI proposal applies continue to reload automatically.
- Hardened reload/apply recovery so generation counters advance beyond the
  supervisor's applied state, workspace bytes and revisions come from one file
  descriptor, unexpected work becomes indeterminate, and startup reconciles
  abandoned running operation records.
- Enforced the action schema's explicit `approved: true` proposal-apply guard
  and terminalized unexpected or malformed proposal-preview visual outcomes.
- Added isolated pre-approval proposal screenshots at a default 1080×1080
  viewport and 1× device scale, with bounded overrides and actual retained PNG
  dimensions recorded in the visual report.
- Fixed successful proposal preview responses creating a circular reference
  between their response envelope and durable operation result, which hid
  already-captured before/after artifact paths from clients.
- Added exact Node 22.23.1 tooling to the platform development container so
  cross-language workspace-fingerprint checks run instead of skipping.
- Added non-persisting authoritative proposal checks, stable validation/SQL
  diagnostics, and checked-operation fingerprints verified again during
  proposal creation.
- Staged the deployable platform separately from the standalone configuration
  CLI.
- Defined `instance` as the home for versioned workspace seed, XYZ settings,
  ETL manifest, and public assets.
- Defined `var` as the ignored home for live workspace, control, proposal,
  audit, durable operation, artifact, reload, and isolated preview state.
- Moved the workspace schema into the configuration service source boundary.
- Narrowed service mounts so XYZ cannot read control-plane state and the
  browser runner uses a dedicated internal automation network.
- Added a production Compose overlay, environment-key doctor, local test entry
  point, Node lockfiles, and per-component Docker ignore files.
- Prevented the configuration static server from resolving parent paths or
  symlinks outside its built asset directory.
- Upgraded Playwright to 1.61.1 after dependency audit identified the
  certificate-verification advisory affecting 1.55.0.
- Added platform-focused architecture, deployment, operations, security,
  backup, API-contract, repository-split, contribution, and licensing
  documentation.
- Verified that the pinned GEOLYTIX XYZ source revision is MIT licensed while
  retaining the separate owner review for MAPP licensing, bundled assets,
  datasets, and release notices.
- Aligned locale selection with XYZ: the top-level `locale` remains the
  default, named `locales` are composed alternatives, and XYZ-specific object
  and array merge rules are preserved. A missing raw default produces XYZ's
  empty synthetic locale rather than auto-selecting a named alternative.
- Expanded schema and validator compatibility for partial locale overrides,
  template and external layers, inline features, zoom-keyed table/geometry
  mappings, external map styles, tile templates, and icon arrays.
- Made advanced/external layer database controls read-only in the dashboard
  while preserving their full XYZ JSON.
- Made composed named locales inspectable in the dashboard and testable through
  the API/CLI, but read-only throughout dashboard mutation controls so
  inherited layers cannot be flattened or apparently deleted; focused
  raw-override changes remain available through proposals.
- Added a server-composed effective-layers endpoint and explicit contract
  capability so independently released clients never need to reproduce XYZ
  locale merging or assume that an older server implements the route.
- Made visual-validation failures retain their plan, report, and authenticated
  artifacts in a structured HTTP 422 response.
- Made save/reload timeouts report committed workspace state in a structured
  HTTP 504 response instead of presenting the result as an ordinary failed
  write.
- Moved component Python suites, frontend tests/build/audit, and JavaScript
  syntax checks into controlled Docker environments; direct helper-suite runs
  use the exact Node release supplied by the development containers.
- Added configured UID/GID ownership preflight checks for mutable state and
  aligned the writable application processes with that identity.
- Hardened the XYZ supervisor with serialized child restarts, TCP readiness,
  atomic status updates, startup timeout handling, and exact workspace-byte
  fingerprints.
- Added a recoverable `applying` proposal transition so an interrupted process
  can reconcile an exact committed candidate before recording `applied` or a
  true revision conflict.
- Added reviewed per-layer ETL minimum source counts so an implausibly small
  but internally consistent source response fails before page loading or
  deletion reconciliation.
- Bounded browser-runner concurrency with a default of one active visual test
  and a hard configuration clamp of one to four; excess internal requests
  receive HTTP 429 instead of starting another Chromium run, and the
  configuration API propagates that status with the selected plan.
- Consolidated the runtime PostGIS connection as `DBS_MAPP`, shared unchanged
  by XYZ and the configuration service, and added explicit bundled/external
  database modes. The Leeds ETL is now identified and guarded as optional
  bundled sample-data provisioning.
- Isolated PostGIS and ETL in the bundled Compose overlay so external mode has
  no sample-service or credential dependency, rejects bundled/loopback
  database hosts, and verifies the running services use the currently resolved
  database URI with bounded connection/query probes.
- Made the deployment environment persistent in `.env` and documented Caddy's
  direct ports 80/443, automatic HTTPS, redirect, certificate-state, and
  production binding validation as the primary live-server topology.
- Made the selected env file authoritative for deployment/database topology,
  rejecting conflicting shell exports, and made production verification pin
  both public hostnames to the local Caddy listener while checking exact HTTPS
  redirects.
- Added a one-year HSTS policy to both public routes without committing sibling
  subdomains or the domains to browser preload lists.
- Installed the system CA bundle in the unmodified XYZ runtime wrapper and
  documented its explicit shared `sslrootcert` path so Node `pg` and
  libpq/psycopg validate external PostgreSQL TLS consistently.

### Security

- Documented the requirement that XYZ cannot read control-plane state.
- Added scoped, expiring device credentials with server-enforced inspect,
  propose, visual, apply, and reload boundaries while retaining legacy
  full-token compatibility.
- Generate a device token only on the first approved poll and atomically store
  its hash with the consumed authorization state; approval never persists a
  raw usable credential. Startup purges any earlier staged raw device record
  and revokes its matching orphan token.
- Isolated pending-proposal preview rendering in a dedicated, non-public XYZ
  process with separate workspace and reload state.
- Enforced strict JSON request/response handling, strict RFC 6901 pointer
  escapes and array indices, bounded request bodies, atomic state writes, and
  inter-process locking for authentication/control state.
- Prevented administrator passwords from being passed as process arguments
  during initialization.
- Bounded audit growth and record size and tightened modes for authentication,
  audit, proposal, and visual-artifact state.
- Limited concurrent browser validation to reduce accidental Chromium resource
  exhaustion; artifact retention and total-size quotas remain an operational
  follow-up.
- Added production validation for distinct public DNS hostnames, direct HTTPS
  on port 443, non-wildcard allowed hosts, and a monitored ACME contact.
These entries describe the staged structure and intended invariants. They do
not claim that final split histories, production deployment, or restore paths
have been validated.
