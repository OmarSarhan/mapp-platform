# Approved mutation admission

## Independent-review waiver — 2026-11-21

The user explicitly waived the outstanding OMA-114 independent-review gate
after being told that the review had been service-blocked and that a waiver
would accept the risk of proceeding without independent sign-off. This gate
is now **waived by the user**, not passed. The blocked review produced no
findings or acceptance, and the service-access restriction is unchanged.

The existing implementation and test evidence remain applicable. Both
maintained wire fixtures were rerun successfully on `3daaf7e` on 2026-11-21;
the acceptance ledger records incomplete expiry/disconnect cross-product
coverage and the limits of simulated handler loss and fixture preview evidence.
The waiver accepts the absence of independent review; it does not establish
untested behavior or waive other requirements, known defects, or deployment
checks. It does not authorize merge or deployment or by itself declare OMA-76
ready. Earlier statements below that this review remains a blocking gate are
historical and superseded by this waiver.

OMA-114 is a local implementation checkpoint on parent `3ad47cf`.
**OMA-76 remains NOT READY.** OMA-120 implements the HTTP and MCP mapping
on parent `a69a414`. OMA-114's independent review was service-blocked before findings.

Config authenticates the caller, checks scope and audience, rebuilds the
exact request digest and redeems token B at ingress. An eligible receipt
permits private preflight. Only the conditional PostgreSQL spend admits a
protected action. It checks the digest, approved decision, revocation and
receipt lifetime again under the row lock. A subsequent budget check can
still prevent the target from starting.

Direct reload spends under the save/reload mutex, reload mutex and mailbox
flock. Creating the coordination directory or lock file does not publish a
generation. Expected workspace metadata precedes generation publication.
Apply acquires lifecycle, proposal and save/reload locks in order, validates
the revision and records the first proposal transition before replacing
the workspace. The save helper checks inside the save lock before intent
or backup, and again before replacement. If a registry needs lazy
initialization during validation, its schema write also requires authority.
Cold initialization can therefore consume a receipt before the target starts.

## OMA-120 interface

Inside the config request scope use `mutation_state().facts()` and
`mutation_state().retryable`. The handler retains that request's state as
`_effects`. The snapshot contains only public correlation fields and
observed target progress.

| Field | Interpretation |
| --- | --- |
| `tokenB` | `not_submitted`, `refused`, `unconfirmed`, `spent` for an acknowledged single-use redemption, or `redeemed` for an acknowledged reusable binding. A broker-client refusal can leave the caller's credentials valid. |
| `receipt` | `not_required`, `unchecked`, `pending` after private validation, `refused`, `unconfirmed` during a possible spend, or `consumed` after acknowledgement. A later file audit failure preserves the acknowledged spend. |
| `targetStarted` | The service reached target submission. An interrupted submission may have committed. A proven refusal of the current SQL command preserves every earlier effect. |
| `published` | Observed workspace revision and/or requested reload generation. A requested generation still needs supervisor acknowledgement. |
| `recovery` | Approval reference, validated request ID and available operation/target identity. Direct operation targets include the fingerprint and proposed generation or the proposal ID, original revision and candidate hash. |

On capacity or deadline refusal with an unstarted target and surviving
authority, a manual fresh exact exchange may proceed while the approval
remains valid. A consumed receipt with an unstarted target has
`retryable=false` and `indeterminate=false`. Inspect a spent or uncertain
authority before seeking a new decision. A possibly committed write has no
automatic replay. Carry the recorded state through the clients, including
when an interrupt reaches a later command. Read-only POSTs, the no-save
mutate branch and a pending approval claim can finish with no target write.

The existing approval reference is known before submission. The winning
UPDATE also inserts a keyed event into the existing control audit table in
the same SQL statement. Authorized store inspection by approval reference
returns its recovery fields even if the process stopped before file audit or
operation creation. For a direct reload or apply, the first 32 hex characters
of that reference name the operation file. Authorized callers can read
`GET /api/operations/{id}` and compare the stored approval reference and
target with the proposal, workspace revision or XYZ status. Older approvals
can have no linked event. A missing or pruned operation file alone cannot
establish that a target never ran.

## Isolated regression

The independent review recorded a 400 ms approved reload that spent its
receipt behind a held lock and published generation one after release.
At the implementation parent, the ingress spend still preceded those locks.

The maintained regression uses real loopback OAuth and config HTTP, a
socket-only PostgreSQL 18 database, an eight-connection role and a separate
reader of durable state. On a private `/tmp/oma114-*` cluster on port 55484,
run `python config-ui/tests/wire_mutation_admission.py SOCKET_DIRECTORY`.
The fixture verifies the cluster and creates and drops its own database.

- With each of the three reload locks held, both expiry and peer closure
  cleared the handler before unlock. B was spent, the receipt stayed
  approved, and the operation set, metadata and requested generation stayed
  unchanged after release. The expiry budget was 450 ms with a 200 ms test
  tolerance. The disconnect bound was 200 ms from socket closure.
- Two fresh exchanges both passed private eligibility for the surviving
  receipt. One received 200 and one received 403. One operation and one
  generation persisted. Binding, wrong audience, stale fingerprint, receipt
  expiry while row-locked, revocation while queued and replay were refused.
- Apply refused while lifecycle, proposal or save/reload admission was
  blocked. After receipt acknowledgement and before independent file audit,
  the pre-submission reference recovered the target identity from PostgreSQL.
  After operation creation and before writing `applying`, interruption left
  the original revision and pending proposal intact.
- After replacement, simulated handler loss left the exact candidate on
  disk, the proposal `applying` and the correlated operation inspectable.
  Startup recovery marked the operation indeterminate and replay left the
  saved revision intact. This test discards the handler stack. It does not
  simulate power loss.
- Login, approval create/confirm/claim, proposal check, mutate preview,
  save-lock refusal and expiry during reload acknowledgement exercised the
  real handler. A direct reload interrupted after publication retained its
  generation and an indeterminate operation.

These tests use a fixture supervisor acknowledgement and synthetic retained
preview evidence. Deployed XYZ, browser rendering, upstream MCP wire
translation and production resource behavior remain separate gates.

## OMA-120 wire regression

Run `config-ui/tests/wire_admission_outcomes.py SOCKET_DIRECTORY` with Python
and the service dependencies. It accepts a private socket-only PostgreSQL 18
cluster in a unique `/tmp/oma120-*` directory on port 55485 with an
`oma120` login limited to eight connections. It creates and drops a disposable
database and runs the mutation regression above before exercising real config
and OAuth HTTP handlers and the Unix-socket MCP server.

The fixture holds three control leases for identity and eight reader leases
for catalog and authenticated SQL test. Each request has a 250 ms budget and
200 ms of scheduler tolerance. It validates the complete response and timing
while the holders still own the leases and requires a fresh 200 after release.
Catalog discovery uses fixture-only PostGIS type signatures with no spatial rows.
A table lock exercises OAuth SQL cancellation. Injected exchange and redemption
500s preserve unavailability. A later acknowledged redemption reports its known
spend even when the config deadline expires during reply processing.

The approved reload cases compare MCP results with the receipt event,
operation file and published generation. Refusal of a later command preserves
an earlier receipt spend or generation publication. Expiry during audit of a
terminal success preserves the acknowledged operation. Separate MCP regressions
expire local handling after a complete framed reply and after receipt claim.
The caller can inspect the public approval reference without access to the
one-time receipt. These cases exercise manual recovery without replay.

### OMA-120 local evidence

The saturated identity GET on the parent closed with
`http.client.RemoteDisconnected`. On the implementation, identity, catalog
and SQL test returned complete JSON 503s in 251, 252 and 252 ms respectively.
Each returned the supplied request ID in the header and body. Local queue
times were 250, 247 and 247 ms. All three recovered with 200.

Verification used PostgreSQL 18.6, Python 3.14.4 and psycopg 3.3.4. These
commands ran at the repository root with the existing virtual environment.
The config suite used a separately created disposable database on the same
private cluster. Temporary state and service logs used the fixture directory.

```sh
py=/tmp/oma76-venv/bin/python
"$py" config-ui/tests/wire_admission_outcomes.py /tmp/oma120-ruPOILHl
PYTHONPATH=config-ui:config-ui/tests "$py" -m unittest test_app_routes test_approvals test_control_plane test_draft_routes test_reload test_token_b_validation test_mcp_token_client_deadlines test_route_deadlines test_lease_deadlines test_derived_layers test_federation_store
PYTHONPATH=mcp-auth:mcp-auth/tests "$py" -m unittest test_introspection test_exchange test_redeem test_control_capabilities test_wire_authorization test_authorization_flow
PYTHONPATH=mapp-mcp:mapp-mcp/tests "$py" -m unittest discover -v -s mapp-mcp/tests
PYTHONPATH=mapp-mcp:mapp-mcp/tests "$py" -m unittest test_config_api_client test_recovery_diagnostics test_exchange_client test_request_lifetime test_approval_gate test_layers_tools
git diff --cached --check
```

The wire regression passed. The config module run passed 730 tests and the
OAuth run passed 170. MCP discovery ran 482 with 479 passing and three
skips for external Codex-client and sibling CLI fixtures. The focused MCP
rerun passed 346. The config suite emitted psycopg resource warnings at
interpreter exit. The cluster reported zero fixture-role sessions before
cleanup. The fixture database, role, cluster directory and raw logs were removed.

## OMA-114 local verification

PostgreSQL 18.6, Python 3.14.4 and psycopg 3.3.4 ran with private filesystem
paths and fixture-only control DSNs. The following commands ran at the
repository root with the existing virtual environment and
`PYTHONPATH=config-ui:config-ui/tests`.

```sh
python -m unittest test_app_routes test_approvals test_control_plane test_draft_routes test_reload test_token_b_validation test_mcp_token_client_deadlines test_route_deadlines test_lease_deadlines test_derived_layers test_federation_store
python config-ui/tests/wire_mutation_admission.py /tmp/oma114-njizPScn
git diff --check
```

The module run passed all 729 tests in 31.630 seconds. The wire regression
passed. For the outer reload mutex, inner mutex and file flock respectively,
the complete expiry cases took 455, 455 and 455 ms. The peer-close cases
took 45, 48 and 46 ms including ingress and state inspection. In each case
the fixture observed handler completion while it still owned the blocker.
The whitespace check passed.
