# MCP authorization threat model

Companion to [ADR 0001](adr/0001-mcp-authorization-and-topology.md). Covers the
authorization component, the credentials it issues and the configuration API's
validation of them, as built in Phase 0.

Every control below names where it is enforced and where that enforcement is
pinned. Where there is no control, the row says so rather than describing an
intention — a threat model whose mitigations are aspirations is worse than none,
because it stops people looking.

**Read this as covering an unmerged feasibility spike.** `mapp-mcp` now exists
and a real MCP client has driven it end to end, so the request digest and the
execution binding have a producer and are exercised rather than assumed. What
remains unbuilt is the approval flow: P8's receipts and review packets are Phase
1, so the approval-forgery row below still describes a control that is designed
and not yet built.

**Accepted by the owner for this version**, with the two unmitigated rows below
— approval fatigue in full, client attestation in part — carried knowingly.

## Assets

| Asset | Where it lives | Compromise means |
| --- | --- | --- |
| Operator credential | `control.admin_credential`, one row, pbkdf2-sha256 at 310,000 rounds | Consent can be granted for any client and scope |
| Grants | `control.oauth_grants` | An attacker acts as a consent that an operator approved |
| Token A | `control.oauth_tokens`, sha256 of the raw value | 15 minutes of MCP-audience access, exchangeable for token B |
| Token B | same table, with an operation and digest binding | One configuration-API operation on one request, ≤60s |
| Authorization codes | `control.oauth_authorization_codes` | A token A, if PKCE is also defeated |
| Operator session | `control.oauth_sessions` + `mapp_oauth_session` cookie | Consent on behalf of the operator |
| Client secrets | `control.oauth_clients`, sha256 | Impersonation of the broker or the configuration API to the control listener |
| Workspace and platform state | outside this component | The effect the whole design exists to gate |

Only hashes are persisted for every credential. Raw values are returned once and
never stored, never logged.

## Trust boundaries

1. **Public internet → Caddy.** TLS terminates here in production.
2. **Caddy → the component's edge listener**, over an exclusive Unix socket.
   Caddy publishes exactly four paths; the component's edge route table
   independently contains no `/internal/*` path.
3. **The `mcp-control` network.** The control listener, reachable only by
   `config-ui` and `mapp-mcp`. Every call is authenticated as a
   confidential OAuth client over HTTP Basic; network placement is not treated
   as authentication.
4. **Component → `control` schema**, as `mapp_control`, which can create nothing
   outside its own schema.
5. **Agent → configuration API.** Where a token B is presented and spent.

The socket peer at boundary 2 is Caddy alone, which is why the component trusts
`X-Forwarded-For` on that listener and only there — the TCP listener class sets
`trust_forwarded_for = False`. Caddy overwrites rather than appends the header.

## Abuse cases

### Replay

**Token B replay.** A captured token B is presented again for the same request.
Refused: a mutating operation's token is single-use, and consumption is one
conditional `UPDATE` with the operation and digest as predicates, so two
presentations cannot both proceed. Measured under 16 simultaneous presenters
across 20 trials: exactly one winner every time. A read operation's token is not
single-use by design — there is nothing to spend, and the binding still confines
it to one request.

**Token B on a different request.** A token minted for one proposal presented
against another. Refused: the configuration API rebuilds the canonical envelope
from the request in front of it and the digest is a predicate of the spend. A
wrong presentation does not burn the credential, so the legitimate retry still
works.

**Authorization code replay.** Refused: the code is consumed by the statement
that reads it, so N concurrent redemptions yield exactly one token.

**Residual:** a token B captured *in flight* and raced to the API before the
legitimate caller wins the race and executes the operation once. The window is
under 60 seconds and the effect is the one the operator approved, but the
attacker chose the moment. Nothing prevents this; TLS and the internal network
are what stand between an attacker and the credential.

### Confused deputy

**The classic OAuth case** — an authorization server tricked into issuing a
credential for the wrong resource. Addressed by audience separation: token A
carries the MCP resource, token B the configuration API, and the component
refuses to start if the two values are equal. Introspection compares the
audience, and the configuration API checks it again on every call.

**Exchange as a deputy.** The broker holds credentials that can mint token B.
Restricted by: an allowlist of five operations rather than all fifty-two; a
requested scope that must be a *subset* of both the token's and the grant's,
never an intersection; and `full` on a hard deny-list so an unclassified route is
unreachable.

**The configuration API as a deputy.** It holds a client secret for the control
listener and could redeem any token B it was given. It only ever redeems against
a digest it computed itself from the request in front of it, so it cannot be
persuaded to authorise a request it did not receive.

**Residual:** the broker is trusted not to fabricate a digest. It cannot verify
one — it never sees the downstream body — so a compromised broker could mint a
token B bound to a digest of its choosing and then present the matching request.
The allowlist and the grant's scopes bound what that could reach. This is
inherent to the split and is the strongest argument for keeping the allowlist
small.

### SSRF

The component makes no outbound requests. The configuration API makes one, to a
fixed internal endpoint derived from `MCP_AUTH_URL`, which is validated at
construction as an internal HTTP root URL with no credentials, query or
fragment, and redirects are rejected by a custom opener.

**Residual:** `MCP_AUTH_URL` is operator-supplied. An operator who pointed it at
a hostile host would send it token values. O15 — whether the component gets a
dedicated egress proxy — remains open.

### Credential theft

Only hashes at rest. Raw tokens are returned once. The consent and login pages
carry a nonce-based CSP with `default-src 'none'`; the session cookie is
`SameSite=Lax`, `Path=/oauth`, `HttpOnly`, and `Secure` in production.
Never logged: authorization headers, raw tokens, codes, refresh identifiers,
broker assertions, cookies, CSRF values, approval state, raw SQL or workspace
content. The exception log lines added for token-B validation carry the
operation id and the failure reason, never the credential.

`hmac.compare_digest` guards the form tokens and client secrets. Hash lookups
are SQL equality on a sha256 of a high-entropy secret, which is a deliberate
move away from constant-time comparison and is documented at the call site.

**Residual:** `SameSite=Lax` is required — the specification's own diagnosis is
that `Strict` will not ride the agent's top-level handoff navigation — so it is
weaker than `Strict` by necessity. And **O20 is unresolved**: whether
`form-action 'self'` survives the cross-origin redirect the consent POST answers
with is unverified, because the Phase 0 harness enforces no CSP. This needs a
manual check in three engines before any public route.

### Approval forgery

The consent screen binds its CSRF token to the session (`session_hash`) and
rotates the token when the session binds, which closed a cross-session consent
forgery where the CSRF check was self-verifying. The consent POST rebuilds the
authorization request from the *stored* query string, so a submission cannot
alter `scope`, `redirect_uri` or `client_id`. A grant records exactly what was
approved, and the exchange checks the grant rather than only the token — a grant
that consented to nothing mints nothing.

Redirect URIs are matched exactly. No prefix or wildcard matching, ever: a prefix
test would admit `<registered>.attacker.example/steal`, which is a working open
redirect that delivers authorization codes.

**Residual:** P8's approval receipts and the review-packet flow are Phase 1.
Phase 0 has consent, not per-effect approval, so a grant with `apply` authorises
any `apply` operation the allowlist permits for its lifetime — the *token* is
request-bound, the *grant* is not.

### Credential administration from a browser session

New in this revision, and a real change to what a web session can reach. An
administrator can now register an agent client and revoke a consent from the
dashboard; previously registration needed shell access to the host and
revocation had no operator surface at all. So the set of privileged actions
reachable by riding an authenticated session has grown.

What bounds it:

- **Administrator session only.** Every route checks the session actor and
  refuses a bearer token, including `full` — the widest credential the platform
  issues. Credential administration is deliberately not delegable to a token,
  and the check is written out per route with a test per route, plus one that
  derives the set of administrator reads from the dispatch so a new one cannot
  be added unguarded.
- **Registering a client grants nothing.** This is the property that keeps the
  new surface from being privilege escalation: a registered client can ask for
  consent and can do nothing until an operator signs in, is shown the scopes and
  approves them. An attacker who registers a client has created something that
  still needs the operator credential to become useful, and the registration is
  in the audit log.
- **`full` and `admin` are refused at registration**, so a client cannot be
  created carrying the scopes the broker's deny-list exists to stop.
- **Revocation fails safe.** The worst an attacker achieves by revoking is
  denial of service — consents stop working and operators re-consent. It removes
  authority and never grants it, which is the direction an attacker does not
  want and an operator under attack does.
- **Both actions are audited** with the acting surface distinguished: `admin`
  for the dashboard, `local-admin` for the command line, so the log answers
  which route was used and not merely that it happened.

**Residual:** these routes inherit the dashboard's CSRF and cookie posture
rather than adding their own, so they are exactly as strong as the session that
reaches them — `SameSite=Strict` on the configuration origin and an
`X-CSRF-Token` on every state change. A session compromise is already total for
the dashboard; what this adds is that it now also reaches agent registration.
The mitigation for that is not in this component but in the operator credential,
and P20 already records that separation of duty is not enforceable with a single
shared administrator identity.

### A second protocol era is served

P2a admits the `2025-11-25` handshake alongside `2026-07-28`. Serving two eras
is the kind of change that usually widens a surface, and here it did not, for
reasons worth recording rather than re-deriving later:

- **The allowlist got stricter, not laxer.** The SDK behind the guard negotiates
  an `initialize` to whatever the client offers — 2024-11-05 verbatim, and an
  unrecognisable offer counter-offered 2025-11-25 — so admitting the method
  without policing it would have admitted four revisions and a fallback. The
  guard reads the offered revision out of the body and refuses everything that
  is not the single admitted handshake revision.
- **No session identifier is minted.** The handshake era is session-based, so
  the obvious cost would have been accepting `Mcp-Session-Id` and the state
  behind it. Measured across a full legacy session — initialize, notification,
  list, call — none is emitted, and the guard strips the header from anything
  the inner application sends regardless.
- **Authorization is untouched by era.** The guard runs before authentication,
  every scope check is era-independent, and no credential, audience or binding
  rule differs between the two.

### State exhaustion

Parked authorization records are capped at 64 per source with a global backstop
of 10,000, and a refusal answers `503` with `Retry-After` rather than a server
error. This replaced a global-only cap that measured worse than none. Measured:
64 admitted, the next 10 refused, and a second source still admitted throughout.
Expiry cleanup runs opportunistically on write; under 16 concurrent writers and
4 concurrent sweeps it removed 100 expired records, removed no live record, and
did not deadlock.

Token B minted per grant is capped at 60 in a sliding 60-second window, which
closes the one unbounded multiplier: every other bound was per credential, so a
grant minting them in a loop was the only way one consent became an unlimited
number of consequential effects. Per grant rather than global, for the reason
the parked-request cap is per source — a global bound lets one busy actor deny
everybody. Refuses with `slow_down`, so a legitimate burst is delayed.

Login attempts are throttled per remote address. Request bodies are bounded at
16 KiB on the control endpoints and 512 KiB at the MCP origin in Caddy.

**Residual:** `control.tokens` is never purged, deliberately — a revoked token's
case-insensitive name must stay reserved. And the connection ceiling of 8 with no
pooling means a burst of concurrent authorization requests is refused by
PostgreSQL rather than queued; measured to recover as soon as a connection is
released, but it is a ceiling and not headroom.

### Approval fatigue

**No control.** P8's time-boxed standing approval and its seven bounds are
Phase 1, and O19 records that neither the capabilities response nor the manifest
can currently express which action classes a standing window may cover. Phase 0
prompts for consent per authorization request, which is the safe end of the
trade-off and also the one most likely to train an operator to click through.

Nothing here mitigates it. Recorded so the Phase 1 design does not treat it as
solved.

### Compromised client

A registered agent client that turns hostile. Bounded by: scopes it was granted
and no more; `full` and `admin` never issued for the MCP resource; no direct
saves, no credential administration, no token issuance or revocation, and no
dashboard or audit administration exposed; every consequential operation
requiring a fresh token B bound to one request.

Disabling a client takes effect immediately at introspection, at the exchange,
and for an already-issued token B — the last of which required checking the
*grant's* client rather than the token's, because a token B names the broker.

Revoking the grant invalidates every credential derived from it at once,
including a token B issued and not yet spent.

A client is registered by an operator, never by itself. Registration validates
the redirect URIs the authorization server will later match exactly, and
refuses `full` and `admin` outright.

**Residual, and accepted:** there is no client attestation. An agent is a
public client — it holds no secret at all, and PKCE binds the code to the
requesting instance rather than proving which application it is.
`private_key_jwt` is deferred for a concrete reason recorded in the ADR. So a
hostile application that can register itself as an agent, or take over a
registered agent's redirect URI on the operator's own machine, is bounded only
by the scopes that agent was granted. P2's client acceptance matrix is 1 of 3,
so nothing can be said about how Codex/OpenAI or Gemini behave.

## Delta: a fourth peer on `mcp-control`

The specification asks for a renewed transport threat model whenever another
service joins that network. This is a recorded delta rather than a renewal, and
saying which is the point: the original analysis is not re-derived here, so a
reader should treat the two together rather than this alone.

`mapp-mcp` has joined, as P12 requires. This was written before it did; the
claims below are now checked against the running deployment rather than
predicted, and each is marked where that changed the evidence. What changes:

- **One more peer can reach the control listener.** It authenticates as a
  confidential client like config-ui, so the listener's admission rule is
  unchanged; what grows is the number of processes holding a credential that
  passes it. The credential is registered by an operator and stored as a digest,
  so compromising the authorization component does not yield it and compromising
  `mapp-mcp` yields only its own.
- **It reaches nothing else.** No `backend`, so no database, and no `edge`. It
  holds no database credential by design and reaches platform state only through
  authenticated API calls, which is the constraint the whole topology rests on.
  *Checked on the running container: it is attached to `mcp-control` and to no
  other network, and its environment carries no database URL of any kind.*
- **Its public surface is a Unix socket**, mode 0660, in a directory shared with
  Caddy alone. *Checked: `srw-rw----`. Worth checking rather than asserting,
  because uvicorn hard-codes `uds_perms = 0o666`; the component binds the socket
  itself to avoid that.* That is the same shape as the authorization component's edge
  socket, and it is why `mapp-mcp` needs no edge network: nothing can reach it
  from a network at all, so a misconfigured route cannot expose it.
- **What it can do with its credential is bounded by what the listener offers**:
  introspect a token A, and exchange one for a token B against an allowlisted
  operation. It cannot mint a token A, revoke a grant belongs to the operator
  surface, and the exchange refuses anything the presented grant does not
  already carry.

One of those questions has since been answered rather than left open. The
listener used to permit any authenticated confidential client everything it
offered, so the configuration API's credential could mint an execution token
for an allowlisted operation -- the privilege the exchange exists to gate, held
by the component with the largest attack surface on the platform. Each client
now carries the capabilities it needs and is refused the rest: the configuration
API may introspect, redeem and revoke; the runtime may introspect and exchange.
Neither can do the other's job.

What this delta still does **not** cover, and a renewal would: whether three
peers on one internal network is the right shape at all. That becomes worth
asking if a fourth arrives.

## Attacker capabilities the design does not defend against

Stated plainly, because a threat model that implies otherwise is misleading:

- **A compromised host or database.** Every credential hash, the operator
  credential and all grant state are readable there.
- **A compromised broker.** It can mint a token B for any allowlisted operation
  within a grant's scopes and fabricate the matching digest.
- **A malicious operator.** P20 records that separation of duty is not
  enforceable with a single shared administrator identity, so it is not claimed.
- **Traffic interception inside the deployment.** The control listener is plain
  HTTP on an internal Docker network. The specification's own transport threat
  model raises mTLS for this; it is not implemented.
- **Restore-time credential invalidation.** `recovery_epoch` exists as columns
  and nothing reads them, so restoring a backup reinstates credentials that
  were valid at snapshot time — including ones revoked since. Deliberately not
  wired and not scheduled: the measured cost is an epoch predicate on 46 statements across two
  components, and a single bulk invalidation as a documented restore step would
  close the same hole more cheaply. Accepted as an open hole by owner
  decision, revisited at the end of the project.

## Review status

The abuse cases the Phase 0 gate names are covered above: replay, confused
deputy, SSRF, credential theft, approval forgery, state exhaustion, approval
fatigue and compromised client. Two carry no mitigation (approval fatigue in
full, client attestation in part) and one carries an unverified control (O20).

Two further cases were added after the runtime and the dashboard surfaces were
built, beyond the list the gate names: credential administration from a browser
session, and serving a second protocol era. Neither is unmitigated, and both are
here because the surface changed rather than because the gate asked — a threat
model that only ever answers its original checklist stops describing the system
it is about.

**The owner's acceptance predates those two rows.** It was given for the eight
above; these are recorded, not yet accepted, and the acceptance line at the top
of this document should be re-taken with them in view.

The owner has accepted this document for this version with those two rows
carried knowingly. That acceptance is not a claim they are mitigated — it is a
decision to proceed with them open, and each has a revisit condition in the
ADR. O20 still needs one manual check in three browser engines before any
public route.
