# MCP authorization and review threat model

This document describes the deployed MCP, configuration API, approval,
candidate-preview, retained-artifact, and disposable-derived-relation paths.
It is a companion to [MCP authorization](mcp-authorization.md), the
[API contract](api-contract.md), and
[derived draft cleanup](derived-draft-cleanup.md).

The previous revision was accepted by the owner on 2026-09-18. This restored
revision was updated on 2026-09-23 for asynchronous visual operations,
retrievable screenshots, viewport framing, proposal decline, and disposable
derived relations. Those additions need review before this revision is treated
as a new accepted security baseline.

Every control below names the enforcement point. Where the implementation
deliberately carries a residual risk, the document says so.

## Scope and security properties

The design aims to preserve these properties:

1. An MCP grant reaches only explicitly allowlisted operations and exact
   scopes, with `full` and `admin` never issued for the MCP resource.
2. A downstream credential is short-lived and bound to one canonical request.
   Mutating requests spend it once.
3. Apply-class and database-management effects require a single-use approval
   receipt bound to that request. Proposal creation, proposal decline, visual
   evidence, and read-only planning are deliberate exemptions.
4. No agent credential reaches credential administration, token issuance,
   dashboard administration, or the audit administration surface.
5. A workspace proposal never changes the served workspace until apply checks
   the stored candidate, revision, evidence, and approval again.
6. A disposable derived relation can be deleted automatically only when its
   creation explicitly authorized cleanup and the platform can prove that the
   exact relation generation is no longer protected by live, proposal,
   publication, preview, or PostgreSQL dependency state.
7. A visual request may continue after its initiating HTTP credential expires,
   but only as the already admitted operation. Its durable operation record is
   the pollable authority for the result.

The MCP runtime currently publishes 56 tools backed by 57 allowlisted
configuration operations. Forty-two are reads. Tool listing is filtered by the
grant, while the operation allowlist remains the downstream security boundary.

## Assets

| Asset | Location | Compromise means |
| --- | --- | --- |
| Operator credential | `control.admin_credential`, PBKDF2-SHA256 | Consent, client registration, and standing approvals can be administered |
| OAuth grants and clients | `control.oauth_grants` and `control.oauth_clients` | A registered client or approved scope can be impersonated |
| Token A | Hashed in `control.oauth_tokens` | Up to 15 minutes of MCP-audience access, subject to its grant |
| Token B | Hashed in `control.oauth_tokens`, with operation and digest | One configuration operation for at most 60 seconds |
| Approval request and receipt | `control` approval state | One exact gated effect can be authorized |
| Operator session | `control.oauth_sessions` and browser cookie | Dashboard actions can be taken as the operator |
| Workspace and proposal state | Private control-state files | Served configuration can be disclosed or a reviewed candidate can be altered |
| Derived definitions and draft journal | `derived_layers` schema | Database views can be changed, adopted, or deleted |
| Visual operations and screenshots | Private `var/control` operational state | Rendered map, UI, labels, and feature information can be disclosed |
| Semantic and federation metadata | Configuration and semantic services | Source systems, relation names, and curated meaning can be disclosed or changed |

Raw credentials are returned once. Persisted credentials are hashes. Logs must
not contain bearer values, authorization headers, cookies, approval receipts,
database URLs, raw SQL, or workspace bodies.

## Trust boundaries

1. **Public network to Caddy.** TLS terminates at the deployment edge.
2. **Caddy to MCP and authorization edge sockets.** Only the reviewed public
   routes are published; internal authorization routes are absent from the edge
   router.
3. **The `mcp-control` network.** `config-ui` and `mapp-mcp` authenticate
   to the control listener as confidential clients. Network placement alone is
   never authentication.
4. **Agent to MCP runtime to configuration API.** The runtime exchanges token A
   for a request-bound token B. The API reconstructs the canonical request,
   verifies the digest, and spends mutating credentials.
5. **Configuration API to PostgreSQL.** Workspace reads use runtime-reader
   privileges. Derived DDL uses the constrained derived-owner role, fixed
   `pg_catalog, public` search path, resource ceilings, and the query and plan
   guards described in [derived layers](derived-layers.md).
6. **Configuration API to browser runner.** The API chooses configured
   internal target URLs. The runner admits only configured origins, refuses
   embedded credentials, applies its egress proxy policy, and writes artifacts
   to private operational storage.
7. **Configuration API to control-state storage.** Proposals, operations,
   publication intents, preview leases, and artifacts are untrusted persisted
   inputs when later read. Reads are bounded and reject symlinks or malformed
   state.
8. **Operator browser to administration routes.** Administrator-session and
   CSRF checks protect client registration, grant revocation, standing
   approvals, and other dashboard-only actions.

## Authorization and approval controls

### Replay and request substitution

Token B includes the operation and a digest of the canonical downstream
request. The API rebuilds that envelope from the request it received. A token
for another proposal, path, body, revision, or operation does not match.

A mutating token is consumed by one conditional database update. Concurrent
presentations therefore have one winner. Read credentials are replayable within
their short lifetime because they do not change state, but remain bound to one
request.

Authorization codes are also consumed atomically. Exact redirect-URI matching
and PKCE prevent prefix and code-substitution attacks.

**Residual:** an attacker who steals a token B in flight and wins the race may
execute the already authorized effect once. TLS, private transport, the
60-second lifetime, the digest, and one-time spend bound that risk.

### Confused deputy

Token A names the MCP audience and token B names the configuration API
audience. The authorization service refuses to start if those audiences are
equal. Both introspection and the API verify audience.

The exchange accepts a closed operation allowlist. Requested scopes must be a
subset of both the token and live grant; they are never widened by
intersection. Unclassified API routes require `full`, which the MCP resource
cannot issue.

The API redeems a token B only against a digest it computed from the received
request. The broker does not see the downstream body and therefore cannot
independently prove that digest.

**Residual:** a compromised broker can fabricate a digest for an allowlisted
operation within a grant and then submit the matching request. Keeping the
operation allowlist closed and the grant scopes narrow bounds this inherent
split-service trust.

### Approval forgery and fatigue

Consequential operations require a receipt bound to the same operation and
canonical request digest. The platform decides whether an operation requires
approval from the registered action risk, with a fail-closed default for new
risk classes.

The receipt is not a tool argument and is never returned to the model. The
platform issues it after a person answers an MCP elicitation or an
authenticated dashboard prompt. The prompt is composed from platform state,
not free-form agent text.

There are fifteen minutes to decide and five minutes to claim and spend an
approved receipt. Grant revocation invalidates approved, unspent receipts.

The runtime retains MCP sessions because in-session elicitation needs a
back-channel. A session identifier authorizes nothing without the bearer
credential, and one session cannot answer another session's elicitation.
The protocol guard admits only `2025-06-18`, `2025-11-25`, and
`2026-07-28`; it refuses the standalone event-stream GET.

Measured client declarations remain:

| Client | Measured declaration | Approval path |
| --- | --- | --- |
| Codex CLI 0.155.0 | `elicitation: {form, url}` | URL |
| Claude Code 2.1.276 | `elicitation: {}` | Form |
| Gemini CLI 0.58.0 | none | Dashboard |

These measurements were recorded on 2026-09-18 and do not claim later client
versions behave identically.

An administrator may enable a standing approval for one registered client and
one platform instance. It stays enabled until turned off.
It has no time or action limit.
Enabling requires administrator authentication within 15 minutes.
A standing approval substitutes the decider, never the bound, single-use
receipt. Disabling the client or advancing the recovery epoch closes it.

**Residual:** a standing-approved client may execute every gated operation its
live scopes permit without individual review, except map proposal application,
which requires individual preview-bound confirmation. Turning the switch off cannot
undo an admitted or completed effect. This is the accepted approval-fatigue
tradeoff and is why narrow scopes and database resource ceilings still matter.

**Residual:** form-mode approval trusts the MCP server to relay the person's
answer honestly. The platform cannot observe the client UI. A compromised MCP
server can approve its own request within the grant it already holds.

### Credential administration

Agent credentials have no credential administration, no token issuance or
revocation, and no dashboard or audit administration exposed. Administrator
browser routes require an administrator session and CSRF token. Registering a
client grants no scope; operator consent is still required. Registration
refuses `full` and `admin`.

Revocation can cause denial of service but cannot grant new authority. Client
registration, consent changes, standing-approval changes, and revocation are
audited with their acting surface.

**Residual:** a compromised administrator session is already authoritative for
the single-operator dashboard and can register clients as well. The deployment
does not claim segregation of duties.

## Network and browser abuse

### SSRF and browser navigation

The authorization service does not accept caller-selected outbound
destinations. The configuration API constructs internal service roots from
operator configuration, rejects credentials, queries, fragments, and redirects
where applicable, and does not accept an arbitrary browser URL in an MCP tool.

For visual checks, the configuration API sends a configured live or candidate
XYZ URL to the browser runner. The runner parses it, admits only an exact
configured origin, and refuses URL credentials. Browser egress remains subject
to the configured proxy and destination policy.

Map centre, zoom, proposal identifiers, layer names, and planned interaction
are input to a permitted origin; they do not select another origin.

**Residual:** deployment operators control the internal service roots and
allowed browser origins. A hostile configuration can send service credentials
or browser traffic to a hostile system. Internal traffic is plain HTTP unless
the deployment adds transport protection.

### Credential theft

Consent and login pages use a nonce-based content-security policy. Session
cookies are HTTP-only, path-limited, same-site, and secure in production.
`hmac.compare_digest` protects form tokens and client secrets. Token hash
lookup uses equality over a high-entropy SHA-256 digest.

The recovery-epoch procedure revokes all live credentials from the restored
state before the stack resumes. Backups still contain credential hashes and
authorization state and need equivalent protection.

**Residual:** same-site settings permit the top-level authorization handoff.
Host, database, administrator-browser, or broker compromise remains outside
what hashing and browser headers can defend.

## Visual preview operations and artifacts

### Long-running previews

Visual-test and screenshot requests may set `background=true`. The API
authorizes and records the operation, returns `202` with a 128-bit operation
identifier, and continues on a background worker. A caller polls with
`visual_operations_show`; the original 60-second token-B lifetime does not
need to cover the browser run.

The browser stage is bounded from 10 to 180 seconds. The end-to-end background
watchdog is bounded from 30 to 600 seconds and defaults to 300. Progress and a
terminal outcome are persisted. A restart converts interrupted work to an
indeterminate terminal state rather than reporting success.

The browser runner admits between one and four concurrent runs and returns
`429` when full. Request bodies are bounded, individual browser stages have
deadlines, and operation history retains at most 500 records when terminal
records are available to prune.

Authorization is checked at admission. The worker carries the admitted actor
and request; it does not refresh token B.

**Residual:** revoking a grant or letting the credential expire does not cancel
work already admitted. The configuration service starts a worker and watchdog
thread for every accepted background request before the browser runner applies
its concurrency limit. A burst can therefore consume threads and operation
records even though excess browser work is rejected quickly. There is no global
visual-operation admission counter in the configuration service.

### Artifact retrieval and disclosure

Successful or failed visual runs can retain PNG screenshots. The MCP
`artifacts_image` tool uses the `visual` scope and returns an MCP image
content block, so the caller can display the evidence rather than receiving
only an internal path.

The image tool now defaults to inline content plus a one-hour signed download;
its link-only mode transfers no inline PNG. Issuance uses the same visual scope
and request-bound query check. The uncredentialed download route on the MCP
origin checks a domain-separated HMAC over version, exact path, content SHA-256
and expiry before touching files, then repeats the retained-report and symlink
checks and rejects changed bytes. It serves an attachment with no-store,
nosniff and no-referrer headers. It does not proxy arbitrary URLs.

The signing key is 32 random bytes held only by the single configuration-service
process. Restarting it invalidates links; no new secret file, artifact cache or
public-request database lookup is introduced. Multi-replica deployments would
need a reviewed shared-key design. Links expire after 300 seconds, may be
downloaded repeatedly, and confer access only to the named image. They are
bearer capabilities for that image, **not** platform bearer credentials.
Revoking the issuing grant does not revoke a previously issued link.

Caddy forwards only the narrow download path and strips incoming cookies and
authorization. The download origin is deployment-controlled and must be HTTPS
except for loopback development; it is not inferred from caller-controlled
forwarding headers. API access logging and bundled Caddy request/error logging redact the
capability. Any additional external proxy logging must redact it too. Anyone with the link may access that image during
its lifetime, so the tool discloses that sharing property. There is no global
download admission counter; each read remains subject to the 8 MiB bound.

Legacy `authenticatedArtifactLinks` still require a dashboard session and
reachable dashboard origin; they are explicitly distinguished from downloads.

Retrieval accepts one syntactically bounded run identifier and one filename
from a closed screenshot-name set. Every path component is opened relative to
the artifact root with symlink following disabled. The run's bounded
`report.json` must name the exact artifact, and the file must be a regular PNG
of no more than 8 MiB. The response is no-store.

The visual scope also reads the durable result of a visual operation. Exact
operation and run identifiers are high entropy, but possession is not an
ownership boundary: any valid `visual` grant that learns an identifier can
read that result or retained screenshot.

**Residual:** screenshots may contain map data, labels, clicked-feature
information, hover text, and operator-visible UI. There is no per-layer,
per-proposal, or per-actor authorization after the `visual` scope check.
Browser artifacts currently have no automatic retention or total-storage quota,
so operators must monitor and remove operational artifacts under their
retention policy.

### Disposable derived creation arguments

`derived_layers_plan` and `derived_layers_create` accept the API's closed
`draft` object or the equivalent `draft_expires_in_hours` convenience
parameter. They reject unknown top-level arguments and conflicting forms.
This prevents an MCP client typo or stale schema from dropping the retention
request and turning a disposable preview relation into a permanent one.

### Viewport framing

Feature-focused framing and full-map-area framing use the same effective
locale-layer dataset: fixed filters and configured feature-set or lookup
restrictions apply to counts, extents, representatives, and focus. Numeric
distribution planning returns bounded aggregates, not source rows.

Full-area framing can disclose more rendered features in one screenshot than a
representative-feature view. This is an expected consequence of the visual
scope rather than an authorization expansion; the same configured layer was
already readable and renderable.

## Disposable derived relations

### Enrollment and binding

Automatic cleanup is opt-in at derived creation. The exact request must include
`draft: {expiresInHours, cleanupApproved: true}`, with a retention period of
1 through 168 hours and no other draft fields. The reviewed plan fingerprint
binds that policy. Existing or permanent relations are never retroactively
enrolled.

The database journal records an exact `{name, assetId, generation}` identity.
A proposal may bind at most 64 draft identities. Each draft must be active,
unexpired, newly referenced by the candidate, absent from the original, and
created by the same actor unless an administrator binds it. The proposal check
fingerprint includes the bindings.

Applying the proposal validates the identity and proposal ownership again.
Every workspace publication path first writes a durable publication intent;
after the workspace commit it adopts referenced drafts permanently. Recovery
can finish adoption from that intent.

Active drafts cannot be replaced or refreshed. This prevents a checked
identity from being changed underneath its proposal.

### Decline and expiry

`proposals_decline` is a final state transition for a pending workspace
proposal. It applies no workspace change and needs `propose`, but no new
approval receipt. Declining an apply-approval prompt is not proposal decline
and does not make the draft immediately eligible.

A bound draft becomes eligible when its owner proposal is declined or
cancelled. An unbound or abandoned draft becomes eligible only after its
approved expiry. An applied proposal or any workspace publication that
references it adopts it instead.

The cleanup worker wakes after decline and otherwise sweeps every 60 seconds in
batches of 20. Eligibility is not deletion authority by itself.

### Cleanup proof and blast radius

Cleanup takes an exclusive, nonblocking lifecycle lock. Workspace save,
proposal publication, and preview paths hold the shared side, so deletion
cannot run between their reference check and use. A crash-safe browser lease of
at least 210 seconds and any queued or running visual operation also retain
drafts.

Before deletion, cleanup verifies:

- the live workspace has no exact or unresolved dynamic reference;
- all proposal original and candidate hashes match their stored bodies;
- pending, applying, or conflicted proposals have no exact or unresolved
  reference;
- publication-intent state does not require adoption or reconciliation;
- the journal, definition, semantic asset, and generation still match;
- PostgreSQL reports no dependent object.

Proposal, publication, and operation scans are limited to 1,000 records,
16 MiB per state file, 32 MiB in aggregate, and two seconds. A corrupt,
oversized, symlinked, ambiguous, dynamic, or incomplete scan retains the
relation. The database rechecks identity in its transaction and executes
`DROP ... RESTRICT`; it then archives the semantic profile and records the
terminal journal state. Failures remain journaled and are retried.

The direct deletion blast radius is one explicitly disposable managed view or
materialized view with the exact journaled name, asset UUID, and generation.
`RESTRICT` prevents cascading deletion of PostgreSQL dependants. Permanent
relations, other generations, source tables, and relations without
`cleanupApproved: true` are outside the path.

**Residual:** a credential with `propose` may decline any pending workspace
proposal whose identifier it knows. That can cancel another review and make
the proposal's explicitly disposable relations eligible for cleanup. The
reference, identity, lease, dependency, and publication checks still apply.

**Residual:** the platform cannot see ad hoc SQL issued by external database
clients or an unsaved workspace held only in a browser. Such consumers may
observe a disposable draft disappear after decline or expiry. Disposable
relations must not be used as permanent shared data before adoption.

**Residual:** conservative failures retain data. Corrupt or excessive control
state, unresolved templates, dependency errors, unavailable PostgreSQL, or a
busy lifecycle lock can therefore cause clutter. This is the intended failure
direction.

## Data disclosure

### Read grants

An `inspect` grant exposes configured layer definitions, the relations and
columns they read, proposal history and diffs, the platform contract, and the
workspace schema. `semantic:inspect`, `derive`,
`federation:observe`, `semantic:source`, and `visual` expose separate
classes of metadata or data.

The dashboard's default `analysis` preset excludes both scopes that disclose
systems beyond the configured workspace: `federation:observe` is offered only
through the separate `analysis-federated` preset, and `semantic:source` is
in no preset. Visual artifacts require `visual`.

The response layer withholds credential identifiers from exchanged
credentials, and tool listing omits tools the grant cannot call.

**Residual:** a grant that holds a scope sees everything that scope covers.
There is no per-layer, per-asset, per-proposal, per-artifact-owner, or row-level
authorization. An instance whose workspace, catalogue, or rendered map is
sensitive should not issue that scope to an untrusted client.

### Expressions and aggregate inspection

`sql_test` evaluates one scalar expression against a configured layer
relation. The transaction is read-only, the statement timeout is five seconds,
the search path is pinned, and allowed functions are checked against shadowing.
Expression syntax, forbidden function checks, and scalar composition prevent
statement injection and unbounded subqueries.

Numeric distribution inspection accepts stored numeric columns and finite,
bounded thresholds or breaks. It applies effective layer restrictions and
returns aggregate summaries rather than source rows.

**Residual:** a valid expression can read any column of a relation the
configured layer already reads. The boundary is the configured layer set, not
the whole database.

## State exhaustion

Authorization requests parked for interaction are capped at 64 per source and
10,000 globally. Refusal is `503` with `Retry-After`. Expired records are
cleaned opportunistically.

Token-B exchange is capped at 60 per grant in a rolling 60-second window.
Login attempts are throttled by remote address. Control request bodies are
limited to 16 KiB and MCP-origin bodies to 512 KiB.

Derived database work has bounded background admission, PostgreSQL role
resource ceilings, statement timeouts, query-shape and plan guards, and a
separate 1 GiB planned-storage guard for materialized output.

Draft cleanup processes 20 journal records per sweep and uses bounded,
deadline-limited state scans. Visual browser concurrency and the remaining
visual-thread residual are described above.

**Residual:** token records are retained so revoked names remain reserved.
Long-lived operational state and browser artifacts need external storage
monitoring and retention. The authorization database connection ceiling
prefers refusal over unbounded queuing under load.

### Blocking I/O and failure recovery

Async mutation and approval tools dispatch synchronous broker/API I/O through
AnyIO's bounded worker pool with caller context propagation. This keeps slow
writes from blocking other MCP sessions or approval traffic on the event loop.
Worker cancellation is shielded while submitted I/O completes; client disconnect
still does not cancel an admitted database operation. Derived mutations default
to durable background work and retain their existing admission and resource caps.

Correlation IDs are validated hexadecimal strings, logged without request bodies,
and confer no privilege. Transport error details contain only a closed cause,
operation, timeout, and request ID. Missing mutation responses remain
indeterminate. Profile metadata queries and semantic connection admission have
bounded waits; these bounds do not constitute a global HTTP-thread admission cap.

## Compromised clients and infrastructure

A hostile registered client is bounded by its live grant, exact operation
allowlist, request-bound token B, and approval policy. Disabling the client or
revoking the grant invalidates derived credentials, including an unspent
token B.

The unattended mutations are proposal creation, proposal decline, preview
evidence, semantic proposal creation, and derived read-only planning. They may
write operational records, and decline may start the already-authorized draft
cleanup path, but they do not publish a workspace. Applying workspace or
semantic proposals, reloading XYZ, changing derived relations, and recording a
federation observation require a request-bound receipt unless a standing
approval supplies the decision.

There is no client attestation. Agent clients are public clients; PKCE binds a
code to the requester but does not prove which application it is. A hostile
application that obtains a registered redirect or operator consent remains
bounded by the granted scopes rather than by application identity.

The design does not defend against:

- a compromised host or PostgreSQL server;
- a compromised broker within its allowlisted operations and live grants;
- a compromised MCP server relaying a false form-mode approval;
- a malicious or compromised single administrator;
- traffic interception on plain internal deployment networks;
- an operator deliberately configuring hostile internal service roots;
- external database consumers relying on disposable drafts without adoption.

## Review status

The restored model covers replay, request substitution, confused deputy,
SSRF, browser navigation, credential theft, approval forgery, approval
fatigue, credential administration, protocol versions, state exhaustion,
read disclosure, expression reads, compromised clients, asynchronous previews,
retained screenshots, viewport framing, proposal decline, and automatic draft
cleanup.

The 2026-09-23 delta expands the documented residual risk in four places:

1. admitted visual work can outlive the credential and lacks a configuration
   service admission counter;
2. retained screenshots are scope-wide operational data with no built-in
   retention quota;
3. a `propose` credential can decline another pending proposal when it knows
   the identifier;
4. cleanup cannot observe external SQL clients or unsaved browser state and
   therefore relies on explicit disposability plus fail-closed checks.

The implementation tests pin the load-bearing counts, scope separations,
approval requirements, disposable-draft enrollment bounds, cleanup scan
bounds, and screenshot retrieval limits in
`mcp-auth/tests/test_threat_model_claims.py`.


### Map confirmation bound to retained evidence

Map proposal application is excluded from standing automatic approval windows.
Its individual confirmation binds proposal ID, candidate hash, original revision,
visual operation ID and a fingerprint of retained original/candidate capture
hashes. The configuration API independently checks the binding and a 24-hour
age limit before writing. A retained rendered candidate map is mandatory;
partial legend/popup/check evidence requires explicit acknowledgment in the
confirmed body and dashboard. Download capabilities expire after one hour and
can be renewed without changing the evidence fingerprint. Missing/replaced
artifact bytes invalidate an older confirmation. This proves which evidence
was offered, not that a human examined every image or that the spatial query is
correct. Study-boundary reports explicitly retain unknowns rather than infer
clipping from arbitrary SQL, layer titles, masks or drawn circles.


Confirmation diagnostics distinguish a client cancellation, client-returned
URL decline, recorded proposal decline, malformed data and unavailable transport.
Cancellation and malformed responses do not create negative approval decisions;
explicit declines close their approval request. Recoverable elicitation failures
can disclose the existing authenticated dashboard approval page; the agent still
cannot decide or manufacture a receipt. Explicit client policy rejections do not
receive fallback instructions. Correlation logs retain modes, bounded response
facts and platform request IDs, never packets, credentials or download links.
These records do not prove that a human saw the client's confirmation UI.
