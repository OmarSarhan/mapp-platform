# Changelog

This project has not yet established a release cadence or semantic-versioning
policy. Entries are collected under **Unreleased** until the owner defines the
first release.

## Unreleased

### Changed

- Staged the deployable platform separately from the standalone configuration
  CLI.
- Defined `instance` as the home for versioned workspace seed, XYZ settings,
  ETL manifest, and public assets.
- Defined `var` as the ignored home for live workspace, control, proposal,
  audit, artifact, and reload state.
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
- Made visual-validation failures retain their plan, report, and authenticated
  artifacts in a structured HTTP 422 response.
- Made save/reload timeouts report committed workspace state in a structured
  HTTP 504 response instead of presenting the result as an ordinary failed
  write.
- Moved platform tests into controlled Docker environments for component
  Python suites, frontend tests/build/audit, JavaScript syntax checks, and
  Compose validation; host Node dependencies are no longer required.
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

### Security

- Documented the requirement that XYZ cannot read control-plane state.
- Documented the current full-scope bearer-token limitation and the need for
  scoped, expiring agent and approval credentials.
- Documented the need to isolate proposal preview from the public live
  workspace.
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
