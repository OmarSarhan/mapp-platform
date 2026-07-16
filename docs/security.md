# Security

This document describes the platform security model and known gaps. For
reporting a suspected vulnerability, see [`../SECURITY.md`](../SECURITY.md).

## Trust boundaries

- Caddy is the only intended public entry point.
- PostgreSQL, XYZ, and the configuration service remain on private service
  networks. The browser runner shares only the narrow automation network with
  the configuration service and Caddy; it has no platform credential.
- The browser runner has a separate outbound network for external map assets;
  it does not join the database, backend, or public edge networks.
- The map and configuration service use separate hostnames.
- The standalone CLI runs on another computer and is untrusted until its
  bearer token is authenticated.
- `instance` contains reviewed non-secret inputs; `var` contains sensitive
  mutable state.

## Filesystem isolation

XYZ must never receive `var/control`. Its readable inputs are limited to the
live workspace and explicitly public instance assets. The configuration
service receives only the writable paths needed for atomic workspace saves,
control records, artifacts, and reload coordination.

This matters because the pinned XYZ version has a local file-provider surface
that this deployment does not need. Caddy blocks its HTTP route as defence in
depth, but a narrow mount remains the primary containment boundary.

Do not store credentials, private source data, API keys, or confidential SQL
samples in the workspace or `instance/public`.

## Authentication and authorization

Dashboard sessions use an HTTP-only, SameSite cookie and CSRF token.
Production must enable secure cookies and HTTPS.

CLI tokens are shown once and stored as hashes by the platform. The current
token model grants full configuration scope. It does not yet separate:

- read and catalog access;
- proposal creation and visual evidence;
- proposal application;
- direct workspace saves and reloads.

Server-enforced scoped, expiring tokens are required before an autonomous
agent can be considered limited to inspection and proposal creation. Written
agent instructions are useful guardrails but are not an authorization control.
Dashboard password changes, token issuance/revocation, and audit access already
require an administrator session and are not bearer-token capabilities.

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
as on the public map route. A separate egress-only network is required because
the live map loads external framework, icon, and basemap assets. The runner
has no database or configuration credentials and does not join the
database/backend or public edge networks. It can address the configuration
service on the shared automation network, so that service must continue to
require authentication for every non-public API. Its output can reveal map
content and failed request URLs, so artifacts are authenticated and must be
protected and expired.

The current visual test renders the live workspace and does not preview a
pending candidate. Candidate preview must not affect public users; a dedicated
isolated process or namespace is still required.

Automatic extent planning can be overridden with a bounded longitude,
latitude, and zoom when outliers or unusual zoom rules produce a poor view.
The runner also caps active Chromium jobs (one by default, configurable only
within 1–4) and rejects excess internal requests with HTTP 429. This limits
concurrent resource pressure but does not replace authenticated access,
artifact retention, storage quotas, or host-level resource controls.

## Secrets

- Keep `.env` mode `0600` and out of version control and image contexts.
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

Production hardening should also include immutable image digests, SBOMs,
vulnerability scanning, host patching, firewall rules, backup encryption, log
retention, and storage monitoring. Where practical, restrict browser egress to
the reviewed asset and basemap origins or mirror those assets locally.
