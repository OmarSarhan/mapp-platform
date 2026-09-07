# MCP authorization component

The `mcp-auth` service is the platform-hosted OAuth 2.1 authorization server
for the forthcoming MCP resource server. It issues the credentials an MCP
client and the internal token broker will use; it serves no MCP tools itself.

**This is Phase 0 of an unmerged feasibility spike.** It is deployed by the
normal Compose model and covered by its own suite, but it has not been run in
production, and one deliberate omission means it cannot yet complete a single
authorization end to end — see [Phase 0 limitation](#phase-0-limitation-nothing-registers-a-client)
below. Read every "designed" statement here as designed-but-unproven unless it
names a test or a check that runs.

## Phase 0 limitation: no agent client can be registered

There is no operator command and no dynamic client registration endpoint —
RFC 7591 registration is Phase 1 work — so no *agent* client can be
registered, and an authorization request from one is refused as an unknown
client. The test suite registers its own, which is why it can prove the flow
works and cannot prove an agent can reach it.

One client is provisioned automatically, and it is deliberately the only one:
the configuration API's own. `ControlStore.ensure_oauth_client` writes that row
at start-up from `MCP_AUTH_CLIENT_SECRET`, because the configuration API is a
resource server calling the control listener rather than a third party asking
for access — and because the service that owns the schema the row lives in is
the one writing it. An agent client is a different thing entirely and must be
registered by a person.

Nothing in this document should be read as an operating procedure for issuing
a token to an agent today. That path is not usable end to end until agent
registration exists.

The other absent consumer is `mapp-mcp` itself, which would obtain a token A
and ask for the exchange. The binding columns are written and read by the
configuration API, and never read outside the
component.

## The third public origin

The platform now publishes three hostnames. `MCP_SITE` is the MCP origin,
defaulting to `http://mcp.localhost` for local work; production sets
`PRODUCTION_MCP_SITE`, which the hardening overlay maps onto both Caddy and the
component. It must be an HTTPS origin on port 443 with a public, resolvable,
non-reserved DNS hostname, distinct from the map and configuration hostnames —
the same rules as `PRODUCTION_MAP_SITE` and `PRODUCTION_CONFIG_SITE`, enforced
by `scripts/validate_production_env.py` and by `compose.production.yaml`, which
fails the deploy outright when the value is unset.

Caddy publishes exactly four paths on that origin:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/.well-known/oauth-authorization-server` | RFC 8414 metadata |
| `GET`, `POST` | `/oauth/authorize` | Authorization request and the consent screen |
| `GET`, `POST` | `/oauth/login` | Operator sign-in for the consent screen |
| `POST` | `/oauth/token` | Authorization-code redemption |

Everything else on the origin is a 404 served by Caddy itself. The MCP resource
itself — `${MCP_SITE}/mcp`, the audience token A is issued for — is not
published at all: it arrives with `mapp-mcp` in a later phase.

The metadata document advertises the authorization and token endpoints, `code`,
`authorization_code`, PKCE `S256`, the two client authentication methods, the
advertised scopes and RFC 9207 issuer identification. It carries no
`registration_endpoint`, which is what makes the missing registration below a
refusal rather than an oversight.

The site block sets no `Content-Security-Policy`. That is deliberate: Caddy's
`header` directive replaces the upstream value, and the consent and login pages
emit their own nonce-based policy. Caddy sets CSP only on the responses Caddy
itself writes.

## Two listeners, two route tables

The component runs two HTTP listeners with disjoint, server-owned route tables.

**The edge listener** binds an AF_UNIX socket at `var/mcp-auth/mapp-auth.sock`
(`MCP_AUTH_SOCKET`, `/run/mapp-auth/mapp-auth.sock` inside both containers).
The wrapper creates the directory; Caddy mounts it and reverse-proxies the four
public paths to it. The socket is created with mode `0660` under the umask
rather than widened after `bind`, so it is never briefly connectable by another
local user. A leftover socket with no listener behind it is removed at start-up;
a live listener aborts the start, and a path that is not a socket is never
unlinked.

Because the socket is exclusive to Caddy, the socket peer is meaningless, so
the login throttle keys on `X-Forwarded-For` — which Caddy overwrites rather
than appends (`header_up -Forwarded`, `-X-Real-IP`, then
`X-Forwarded-For {remote_host}`). The header is trusted on the Unix listener
only; on a TCP listener it is attacker-supplied and ignored.

**The control listener** is ordinary TCP on `MCP_AUTH_CONTROL_PORT` (8080),
published to no host port and reachable only on the `mcp-control` Docker
network. It carries the health check and the four internal endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Container health check; reports unhealthy when the control schema is unreachable |
| `POST` | `/internal/oauth/exchange` | The restricted RFC 8693 exchange that issues token B |
| `POST` | `/internal/oauth/introspect` | RFC 7662 introspection |
| `POST` | `/internal/oauth/revoke` | Grant revocation |
| `POST` | `/internal/oauth/redeem` | Spends a token B against one operation and one request digest |

Every `/internal/*` endpoint authenticates its caller as a confidential OAuth
client over HTTP Basic. Being on an internal Docker network is not treated as
authentication.

The four `/internal/*` endpoints are not reachable from the edge, and two
independent controls make that true:

1. **The route tables are disjoint and belong to the listener object**, not to
   the handler class. The edge listener has no entry for any `/internal/*` path,
   so such a request is a 404 on the edge socket regardless of what Caddy does.
2. **The Caddy allowlist** matches exactly the four public paths and answers
   404 to everything else on the origin.

Both hold, and both are asserted: the component's own tests pin the two tables
as disjoint and check every method against every control path, `./bin/mapp
verify` requests all three `/internal/*` paths through the public origin and
fails unless each answers 404, and `scripts/tests/test_caddy_contract.py` pins
the four-path allowlist and the 404 fallback in the Caddyfile.

The exchange in particular is placed on the internal listener on purpose. It is
the most security-critical surface in the design, and an edge-routed path would
make the Caddy allowlist the only thing between it and the internet.

All three internal endpoints authenticate the caller with HTTP Basic before
anything about a token is looked up, and only a confidential client registered
for `client_secret_basic` is accepted. An unauthenticated caller therefore never
learns whether a token exists, which is the guessing oracle RFC 7662 warns an
open introspection endpoint becomes.

## Networks and container placement

`mcp-auth` joins `backend` and `mcp-control` only. It is deliberately **not** on
`edge`: its public surface is reached solely through the Unix socket, so there
is no network path from Caddy's network to the component at all. `mcp-control`
is an internal Compose network whose only members are `config-ui` and
`mcp-auth`; `backend` is there because the control schema lives in the packaged
database. `scripts/tests/test_compose_isolation.py` pins both memberships and
the absence from `edge`.

The container follows the platform's hardening pattern: read-only root, a
16 MB `tmpfs` for `/tmp`, `no-new-privileges`, all capabilities dropped, a
non-root user, and one mount — the runtime directory holding the socket. It
receives no workspace, no control-plane files and no Docker socket.

In the packaged model the service waits for the database to be healthy before
starting, because every credential it reads or writes lives there. In the
external-PostgreSQL model there is no packaged database service to depend on
and the container health check is the only gate.

## State

The component keeps nothing on disk. Its records live in the `control` schema
of the platform database, reached with `CONTROL_DATABASE_URL` as the
`mapp_control` role, and it fails closed at start-up when that value is unset
rather than falling back to an in-memory store. The schema and its migration
ladder are owned by `config-ui/control_schema.py`; the configuration service
applies the ladder on first use, and the component expects the tables to exist.

Its tables are `oauth_clients`, `oauth_authorization_codes`, `oauth_tokens`,
`oauth_pending_authorizations`, `oauth_sessions` and `oauth_grants`. They share
the schema's two rules: a one-shot record is consumed by the conditional
`UPDATE ... WHERE consumed_at IS NULL ... RETURNING` that reads it, so the
database decides a race rather than a process-local lock; and a consumed record
is marked rather than deleted, so a replay arrives at a row that says when it
was spent instead of looking merely unrecognised.

The store opens one short-lived connection per call and does not pool, which
makes the `mapp_control` role's `CONNECTION LIMIT` of 8 the backstop — and,
since `config-ui` shares that role and also does not pool, a shared ceiling on
concurrent control-plane operations rather than headroom. See
[external PostgreSQL](external-postgresql.md#control-plane).

### The operator credential

The consent screen authenticates the platform administrator against
`control.admin_credential` — the same single credential `./bin/mapp init`
writes and `./bin/mapp reset-config-password` replaces, verified with a hasher
byte-compatible with the configuration service's. It is read per attempt
rather than captured at start-up, so a password change needs no restart.

There is no `MCP_AUTH_ADMIN_PASSWORD_HASH` environment variable. An earlier
revision read one, nothing ever set it, and a correctly deployed component
could therefore authenticate nobody; `scripts/tests/test_compose_isolation.py`
now pins that the credential is never passed as an environment value.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `MCP_SITE` | `http://mcp.localhost` | The public MCP origin Caddy serves and the value Compose feeds to `MCP_ISSUER` and `MCP_RESOURCE` |
| `PRODUCTION_MCP_SITE` | unset | Required for production; replaces `MCP_SITE` on Caddy and the component under the hardening overlay |
| `MCP_ISSUER` | `MCP_SITE` | The issuer identifier in metadata and in the RFC 9207 `iss` parameter |
| `MCP_RESOURCE` | `${MCP_SITE}/mcp` | What token A is for |
| `MCP_CONFIG_API_RESOURCE` | `${CONFIG_SITE}/api` | What an exchanged token B is for. It must differ from `MCP_RESOURCE`; equal values would collapse the audience separation and let a token A be replayed at the configuration API, so the component refuses to start |
| `MCP_AUTH_SOCKET` | `/run/mapp-auth/mapp-auth.sock` | The edge listener's socket path inside the container |
| `MCP_AUTH_CONTROL_PORT` | `8080` | The control listener's TCP port |
| `MCP_AUTH_SECURE_COOKIES` | empty | Compared against the literal string `true`, not truthiness. The production overlay sets `"true"`. Anything else, including `false`, leaves the session cookie without `Secure` |

The configuration API reads three of its own, and refuses every exchanged
token unless all three are set — a credential it cannot ask about is one that
authorises whatever it claims:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_AUTH_URL` | `http://mcp-auth:8080` | The control listener, on the `mcp-control` network |
| `MCP_AUTH_CLIENT_ID` | `mapp-config-api` | The confidential client the configuration API authenticates as |
| `MCP_AUTH_CLIENT_SECRET` | empty | Its secret. Generated by `./bin/mapp init`; blank turns token-B validation off, which refuses every exchanged credential |

`MCP_CONFIG_API_RESOURCE` is supplied to both services from the same Compose
expression, and the configuration API checks it on every introspection. If the
two disagreed, every exchanged token would be refused with nothing to indicate
why. A test asserts the resolved values are equal.
| `AUTHLIB_INSECURE_TRANSPORT` | `1` | Development only: authlib refuses `http://` for anything but literal localhost, and the local origin is `http://mcp.localhost`. The production overlay clears it, and a test pins that it is cleared |
| `CONTROL_DATABASE_URL` | empty in the base model | The `mapp_control` DSN. Supplied by `compose.bundled-db.yaml`; an external-PostgreSQL deployment must set it explicitly |

`MCP_AUTH_SECURE_COOKIES` is compared against a literal because the platform
teaches operators to write `false`, and truthiness would turn `Secure` on for
exactly the operators who wrote it off — over plain HTTP, where the cookie is
then never sent and sign-in silently fails.

## Credentials the component issues

| Credential | Lifetime | Notes |
| --- | --- | --- |
| Authorization code | short, single use | Authorization code with PKCE `S256`, required of every client including confidential ones |
| Token A (`mapp_a_`) | 15 minutes | What an MCP client holds; audience is `MCP_RESOURCE` |
| Token B (`mapp_b_`) | at most 60 seconds | Issued only by the internal exchange, for one allowlisted configuration-API operation against one canonical request; single-use when that operation mutates |
| Operator session cookie | 30 minutes | `mapp_oauth_session`, absolute expiry, for the consent screen only |

Refresh tokens are not implemented: no refresh grant is registered and only the
access token is persisted, so a refresh token issued today could never be
redeemed.

Discovery advertises `mcp:connect` and `inspect` alone, so a greedy client
cannot auto-request every permission from metadata. The acceptance list is
wider — the MCP scopes plus every scope the allowlisted operations require —
and is derived from the operation table rather than restated, so the allowlist
and the issuer cannot disagree.

The exchange refuses a request whose scopes are not a subset of what the grant
permits, rather than intersecting them. Silent intersection would turn a
widening attempt into a working narrower token that the client never learns was
altered.

Revocation is grant-shaped. Revoking a grant invalidates every credential
derived from it at once, including an already-issued token B, because every
path that reads a token resolves it through its grant — introspection, the
exchange, and both halves of the token-B verification surface. Revoking tokens
individually could not mean what an operator withdrawing consent intends,
since the broker can mint another an instant later.

## The operation binding

A token B authorises one operation on one request, and the two components
divide that check because neither can make it alone. The broker validates the
*shape* of the digest it is handed at exchange time and stores it; it never
sees the downstream request, so it cannot recompute anything. The
configuration API rebuilds the canonical `mapp-jcs-v1` envelope from the
request in front of it — version, target instance, upper-case method,
operation id, manifest path template, typed path parameters, normalized path,
ordered query pairs and the exact body — digests that, and presents the result
to `/internal/oauth/redeem`. For a mutating operation the redemption consumes
the token in one conditional statement, with the operation and digest as
predicates, so two presentations cannot both proceed.

Three consequences worth stating, because each is a decision rather than an
accident:

- **The request decides which operation it is, never the token.** If the token
  named its own operation, a credential bound to one proposal could present
  itself as the operation for another. A route that no template matches, or
  that two action ids claim, is refused rather than resolved by precedence.
- **Non-canonical requests are refused, not repaired.** Dot segments, encoded
  separators, lower-case percent-escapes, encoded unreserved characters and
  duplicate query parameters are all rejected. Normalizing twice is how two
  boundaries end up disagreeing about which request they authorised.
- **The credential is not a member of the envelope.** A token cannot be an
  input to the digest that authorises it, so the `Authorization` header, CSRF
  values and trace headers are excluded by construction.

The canonicalizer is vendored into both components rather than shared, because
they ship as separate images with no shared package. Each copy is verified
against the RFC's own vectors independently, and a further test compares the
two copies directly — a vendored copy nobody compares is one that has already
drifted, and a drift here is silent: the broker records whatever digest it is
given, so a configuration API that canonicalizes differently refuses every
token B with no indication why.

Redemption happens after authentication and after the scope check, so a
request refused for want of a scope does not burn a single-use credential.
`_json` is the configuration API's only response helper and refuses to emit a
success for an unredeemed exchanged token, which makes that a property of the
response layer rather than something every handler has to remember. It is a
backstop rather than the gate: by the time a response is written a mutation has
already been applied, which is why redemption happens in `_authorized` before
any handler dispatches.

## Sign-in and consent

The consent and login pages are two plain HTML forms with no JavaScript and no
template engine, which is what makes their `default-src 'none'` policy honest.
Sign-in attempts are throttled at 8 per 300 seconds per client address,
mirroring the configuration service. The operator authenticates, sees the
client and the exact scopes requested, and approves or refuses; the approved
set is recorded as a grant, and a token may never widen beyond it.

## Verification

`./bin/mapp verify` requires `mcp-auth` to be running, requests the metadata
document through the public MCP origin — an unauthenticated response served by
the component rather than by Caddy, so it fails if the socket is missing, if
the control schema is unreachable, or if the issuer is misconfigured — and
then asserts a 404 for each of the three `/internal/*` paths on that origin.

`./bin/mapp test` runs the component's own suite in a container with a real
database and fails the run if any test skips.

The production acceptance evidence run does **not** yet cover this origin: its
DNS and TLS checks read `PRODUCTION_MAP_SITE` and `PRODUCTION_CONFIG_SITE`
only. Confirm the MCP hostname's DNS answer and certificate separately when
recording release evidence.

## Backup

There is nothing service-specific to back up. The records are in the `control`
schema and are captured by the database dump described in
[backup and restore](backup-restore.md). `var/mcp-auth` holds only the socket,
which is recreated on start; do not restore it.

## Not built yet

Phase 0 stops deliberately short in several places. Beyond the missing client
registration above:

- the configuration API does not re-check a token B's operation and request
  binding, so the binding is written and read only within the component;
- `recovery_epoch` exists on every table that can authorise something, but
  nothing writes or reads it, so restoring a snapshot invalidates nothing;
- the operation allowlist covers a handful of configuration-API actions
  rather than the whole surface, and adding one is a deliberate act;
- there is no dashboard view of grants and no operator revocation command;
- the migration ladder is forward-only.
