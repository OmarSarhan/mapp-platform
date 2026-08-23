# Security

This document describes the platform security model and known gaps. For
reporting a suspected vulnerability, see [`../SECURITY.md`](../SECURITY.md).

## Trust boundaries

- Caddy is the only intended public entry point.
- Live mode uses Caddy-managed HTTPS for all application traffic. Public port
  80 exists only for automatic certificate handling and HTTPS redirects;
  application containers are never published directly.
- Both public routes send a one-year HSTS policy after HTTPS is established.
  It intentionally omits `includeSubDomains` and preload until the owner has
  validated every sibling hostname and accepted that broader commitment.
- Bundled PostgreSQL, XYZ, and the configuration service remain on private
  service networks. External PostGIS is reached only from the backend network
  through the operator-approved route. The browser runner shares only the
  narrow automation network with the configuration service and Caddy; it has
  no platform credential.
- The browser runner has no direct outbound network. External map assets pass
  through the dedicated HTTPS-only hostname-allowlisting proxy, which is the
  only service on both the internal automation network and the external
  browser-egress network. Unlisted hostnames are rejected without DNS lookup;
  reviewed names are then resolved and rejected if they map to private,
  loopback, link-local, or reserved space.
- The semantic service shares only the internal `semantic-control` network
  with the configuration service. It has no public route, database network, or
  database credential.
- The map and configuration service use separate hostnames.
- The standalone CLI runs on another computer and is untrusted until its
  bearer token is authenticated.
- `instance` contains reviewed non-secret inputs; `var` contains sensitive
  mutable state.

## Filesystem isolation

XYZ must never receive `var/control` or `var/semantic`. Its readable inputs are
limited to the live workspace and explicitly public instance assets. The
configuration service receives only the writable paths needed for atomic
workspace saves, control records, artifacts, and reload coordination. The
semantic service receives only `var/semantic` as writable state.

This matters because the pinned XYZ version has a local file-provider surface
that this deployment does not need. Caddy blocks its HTTP route as defence in
depth, but a narrow mount remains the primary containment boundary.

Do not store credentials, private source data, API keys, or confidential SQL
samples in the workspace or `instance/public`.

## Authentication and authorization

Dashboard sessions use an `HttpOnly`, SameSite cookie and CSRF token.
Production must enable secure cookies and HTTPS.

CLI tokens are shown once and stored as hashes by the platform. For device
authorization, approval stores no usable credential: the raw token is
generated only during the first approved device poll, returned once, and only
its hash is committed atomically with the consumed state. Legacy `full` tokens
remain compatible. Device-authorized tokens separate:

- read and catalog access;
- proposal creation and visual evidence;
- proposal application;
- reload access.

Device authorization defaults to expiring `inspect + propose + visual` access;
it also includes read-only `semantic:inspect`. Apply, reload, derived-layer
mutation, external semantic generation, semantic proposals, semantic apply,
and semantic administration are explicit additional grants. Direct workspace saves remain
full-token/administrator operations. Scope checks are server-enforced; written
agent instructions remain defence in depth rather than authorization.

A credential with the same `derive + semantic:inspect` authority required to
create a managed derived layer may request bounded category counts for a stored
field on an existing database-backed workspace layer. This does not grant
general `inspect` access or return raw rows: the service accepts one safely
quoted column, caps the result at 500 values, and applies its read-only
five-second database timeout.
Dashboard password changes, token issuance/revocation, and audit access already
require an administrator session and are not bearer-token capabilities.

For CLI-token provisioning, the dashboard offers semantic reader, proposal
author, AI semantic author, curator, delivery operator, semantic administrator,
and full platform operator presets. The custom-scope control can instead
select the exact workspace and semantic scopes required. These presets are UI
conveniences, not role inheritance; server authorization evaluates the stored
scope list. New dashboard and administrator-API tokens default to a 30-day
expiry. The 1-, 7-, and 30-day choices need no additional acknowledgement;
90-day and non-expiring tokens require an explicit extended-lifetime
confirmation that the API also enforces. Prefer the shortest practical
lifetime. The bearer value is shown only once, while its name, scopes, expiry,
use, revocation, and issuance metadata remain available for administration and
audit.

The configuration service is the semantic authorization gateway:

- `semantic:inspect` reads visible catalog, asset history, and proposal state;
- `semantic:source`, together with `semantic:inspect`, discovers and
  synchronizes only allowlisted PostgreSQL catalog metadata through the exact
  configured read-only alias;
- `semantic:generate`, together with `semantic:inspect`, sends whitelisted
  semantic metadata to Gemini and returns a non-persisted draft;
- `semantic:data`, together with both generation scopes, permits an explicit
  opt-in to a bounded 5% row sample and/or data-derived statistics;
- `semantic:propose` checks and creates curated proposals;
- `semantic:apply` applies an approved pending proposal; and
- `semantic:admin` permits a confirmed retry of a retained derived-profile
  event; together with `semantic:inspect`, it also permits source/profile
  archival and exact-ID administrative visibility.

These narrow bearer-token scopes are not hierarchical. Grant only the exact
combination needed; `semantic:generate` alone does not imply inspect or
data-derived egress, `semantic:data` alone does not imply inspect or generate,
`semantic:source` alone does not imply inspect or database row access, and
`semantic:admin` alone does not imply inspect, propose, or apply. The endpoint
named `repair` requeues the same retained event and
payload; it is not authority to rewrite semantic facts or a mechanism that can
correct a deterministic conflict. An authenticated dashboard administrator is
handled separately.

`GEMINI_APIKEY` is optional and is exposed only to `config-ui`. The default
Gemini request contains no source rows or sample values. A caller holding
`semantic:data` may explicitly add an up-to-100-row, 96-KiB sample selected
from 5% of the relation and/or bounded statistics. A table sample is further
limited to 20 eligible columns and 512 characters per serialized value;
geometry and binary values are omitted, and a field request can include only
that field. Field statistics aggregate at most 1,000 rows selected from 5% of
the relation. Raw SQL, unrelated field annotations, bearer tokens, internal
semantic tokens, and database credentials are never sent. The key, prompt,
provider body, sampled values, statistics values, and generated values are
excluded from audit records and client-visible errors. Provider output and all
supplied context are treated as untrusted data, while curated operation paths
are always constructed by the server.

`SEMANTIC_SOURCE_ALLOWLIST` defaults to `MAPP:leeds.*`; an explicitly empty
value disables source discovery. `SEMANTIC_SOURCE_EXCLUSIONS` uses the same
selector syntax to omit internal relations from discovery and synchronization.
System and managed derived schemas are always excluded. Adding an exclusion
does not automatically remove an already-registered profile. The separately
confirmed archive-excluded action requires both `semantic:inspect` and
`semantic:admin`; it archives matching ready profiles without changing their
database relations. Archived assets are omitted from catalog, search, and
derived-profile collections even for administrators and return `404` to an
ordinary exact lookup. Exact asset and immutable-history reads remain
available by a previously retained ID only to an administrator or a token with
both scopes. The same authority can archive one selected ready profile through
the dashboard. Archival cannot be undone by merely removing the exclusion.
Synchronization holds a read-only catalog snapshot and relation lock, checks
current `USAGE` and `SELECT`, and reads only relation/column metadata and
bounded standard comments. It never queries table rows, values, defaults, or
expressions, and it fails closed after privilege loss or relation change.

When combined with `semantic:inspect`, administrative access also adds a
sanitized name-level delivery diagnostic to derived-profile reads. It contains
bounded event metadata and a single-line error, never the retained payload,
worker claim, or database credential. Ordinary semantic inspectors receive
only the public profile state. Unmatched blockers left by an already-dropped
definition are likewise visible only to administrative reads.

The private service accepts a separate internal bearer token plus the trusted
actor and effective scopes forwarded by the configuration service. It is not
safe to publish that service or let a browser supply those internal headers
directly.

## Workspace validation

The platform validates known workspace structure while preserving XYZ
extension properties. Database-backed layers are checked against the live
catalog and the read-only XYZ role. Feature identifiers must be non-null and
unique, and a bounded render probe must succeed before a managed save.

API bodies and responses use strict JSON; non-finite numeric constants are
rejected. Managed operations use strict RFC 6901 pointers with validated array
indices and cannot replace the workspace root. Authentication/control records
and workspace saves use atomic writes, with inter-process locking for shared
control state.

Calculated `infoj[].fieldfx` values are limited to one scalar, read-only
PostgreSQL expression. The validator rejects statements, comments, subqueries,
data or schema changes, transaction/session commands, sleeps, system/file
access, notifications, and database links. Queries use read-only transactions
and a statement timeout.

Expressions can still be expensive, expose sensitive columns, or return data
that should not be displayed. Review every new expression and its result.

## SVG assets

The dashboard offers regular SVG files from `instance/public/svg` only after
size and content checks. Scripts, event handlers, foreign objects, entities,
and doctypes are rejected. Asset provenance and redistribution permission are
separate concerns covered in [`../LICENSING.md`](../LICENSING.md).

## Visual validation

The browser runner receives a platform-generated XYZ URL, layer, view plan,
and viewport. The top-level target origin is fixed to Caddy's un-published
automation listener on port 8081, where the same file-provider denial applies
as on the public map route. External framework, icon, and basemap requests use
the reviewed Squid hostname allowlist; internal Caddy and preview navigation
bypasses the proxy. The runner has no direct external route, database or
configuration credentials, and does not join the database/backend or public
edge networks. It can address the configuration
service on the shared automation network, so that service must continue to
require authentication for every non-public API. Its output can reveal map
content and failed request URLs, so artifacts are authenticated and must be
protected and expired.

Proposal visual tests use a dedicated `xyz-preview` process, private workspace,
and reload mailbox. Only pending, integrity-valid, non-superseded stored
proposals can be rendered. Requests are serialized through browser completion
to keep the candidate stable, and artifacts carry the proposal ID and candidate
hash. The preview process is not public and cannot write or reload the live
workspace.

Automatic extent planning can be overridden with a bounded longitude,
latitude, and zoom when outliers or unusual zoom rules produce a poor view.
The runner also caps active Chromium jobs (one by default, configurable only
within 1–4) and rejects excess internal requests with HTTP 429. This limits
concurrent resource pressure but does not replace authenticated access,
artifact retention, storage quotas, or host-level resource controls.

## Secrets

- Keep `.env` mode `0600` and out of version control and image contexts.
- Treat `DBS_MAPP` and `ETL_DATABASE_URL` as credentials. Do not print Compose
  renderings or diagnostics that expand them; XYZ and the configuration
  service intentionally receive the same read-only runtime URI.
- Treat `DERIVED_DATABASE_URL` as a separate privileged credential. Only the
  configuration service receives it, and its role must own only
  `derived_layers`. Creation and replacement also require `semantic:inspect`
  and ready semantic profiles for every declared relation source; H3 and
  PostGIS functions do not require profiles. Creation, materialized refresh,
  replacement, and drop require the `derive` scope and are audited.
  The mandatory map-extent guard filters final output geometry and is not an
  authorization or source-row boundary. Derived SQL passes a PostgreSQL AST
  policy and a transient-view catalog OID/provenance check before `EXPLAIN`;
  custom, volatile, security-definer, configured, unproved set-returning, and
  administrative routines and custom operators/casts/types are rejected as
  `derived_layer.query_not_allowed`, distinctly from malformed SQL and
  over-budget computation. The materialization planner and post-population
  actual-size guards limit obvious stored-output growth but do not prevent
  transient relation, index, TOAST, or WAL use and do not replace query-cost
  review, database quotas, monitoring, or host resource controls. Durable
  operation errors expose safe user guidance and keep database diagnostics out
  of the primary message; an uncertain commit is marked indeterminate rather
  than claiming the state is unchanged.
- Treat `FEDERATION_DATABASE_URL` as a separate provisioner credential. Its
  role alone owns the `federation` registry, registered `source_<alias>`
  schemas, foreign servers, and foreign tables. It has database `CREATE` and
  `postgres_fdw` `USAGE` solely to create those reviewed objects; the derived
  owner and runtime reader receive only `USAGE`/`SELECT` on active source
  schemas and tables. The provisioner must not own or access `derived_layers`
  or source-data schemas such as `leeds`, and the derived owner must not retain
  database `CREATE` or FDW `USAGE`.
- Treat each `FEDERATION_DBS_<REF>` value as a remote-source credential. The
  configuration service resolves it only for federation Observe/Provision;
  the separate namespace deliberately keeps it out of ordinary `DBS_*`
  catalog, layer, workspace, and semantic discovery.
- A source column using `pg_catalog.default` is importable only when the
  source and federation databases have the same attested provider, locale,
  encoding, and matching recorded/actual version. An unversioned default is
  accepted only for libc `C`/`POSIX`; built-in defaults also require the same
  PostgreSQL major. Explicit `C` and `POSIX` remain portable when database
  encodings match; other source collations are unsupported.
- Every platform caller uses the same mapped remote federation identity.
  Do not register user-private, recipient-filtered, or end-to-end-encrypted
  message/key relations. Catalog RLS detection cannot attest application-level
  filtering; expose only a dedicated remote view that is safe for every caller.
- Federation `active` status is point-in-time evidence from the last explicit
  Observe/Provision, not continuous remote attestation. The manual freshness
  strategy trusts source administrators to preserve approved relation semantics
  between observations; rerun Observe after source schema or view releases.
- Treat `SEMANTIC_INTERNAL_TOKEN` as a service credential. Only
  `config-ui` and `semantic-service` receive it. It must be random, at least 32
  characters in production, and distinct from database and user credentials.
- Treat `GEMINI_APIKEY` as an optional external-provider credential. Only
  `config-ui` receives it. Keep it in the private environment file or approved
  secret manager, use a dedicated restricted key, and review provider terms,
  billing, retention, regional processing, and metadata-egress policy before
  enabling it.
- Store administrator passwords and CLI tokens in an approved secret manager.
- Never pass tokens in command arguments, logs, proposals, screenshots, or
  issue reports.
- Rotate credentials after suspected exposure.
- Scan the canonical Git history before publishing the split repositories.

## Container and network hardening

The application containers use read-only roots where practical,
`no-new-privileges`, dropped capabilities, dedicated non-root users, bounded
temporary filesystems, and internal networks. The configuration service and
CLI do not require Docker socket access; do not add it.
The semantic service follows the same hardening and has only its narrow state
mount. A future data/function executor must be a separate container with its
own least-privilege credential rather than adding database access to the
metadata service.
Initialize and operate production as a dedicated unprivileged host account.
Production validation rejects `CONFIG_UID=0` or `CONFIG_GID=0` so a root-run
initialization cannot silently make the application services run as root.

Production hardening should also include immutable image digests, SBOMs,
vulnerability scanning, host patching, firewall rules, backup encryption, log
retention, and storage monitoring. Keep browser destinations in the reviewed
`instance/browser-egress-allowlist.txt`; mirror an asset locally when its
origin cannot meet the deployment's availability or privacy requirements.
