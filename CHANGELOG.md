# Changelog

This project has not yet established a release cadence or semantic-versioning
policy. Entries are collected under **Unreleased** until the owner defines the
first release.

## Unreleased

### Fixed

- Fixed catalog discovery for PostGIS geometry columns on materialized derived
  layers, allowing dashboard and CLI validation and XYZ map configuration to
  recognize them.

### Changed

- Added atomic, validated derived-layer replacement and kind conversion, with
  structured blocking feedback for PostgreSQL dependents and dashboard
  workspace references.
- Added first-class XYZ layer-folder support through validated per-layer
  `group` values, grouped dashboard navigation, and a layer-folder editor.
- Added first-class controls for XYZ's interactive layer Styling panel,
  including panel visibility and ordered `style.elements`, while preserving
  custom extension keys.
- Added schema validation and dashboard controls for XYZ layer Filtering
  panels, layer-level behavior, and per-information-field filters.
- Made proposal screenshots render a high-resolution, isolated
  original-versus-candidate comparison instead of labelling candidate
  pre-click/post-click images as before/after. Feature-information changes now
  select the same feature on both sides, wait for the expanded information
  panel, and retain full-page and panel-only comparison artifacts.
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
