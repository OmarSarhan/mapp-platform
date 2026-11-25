# OMA-76 implementation and isolated evidence

**Not ready — independent review on 2026-11-20 supersedes the readiness
assessment below.** Connection savings reproduced through real MCP transport,
but origin cancellation, post-wait mutation deadlines, overload response
mapping and the normal database-backed regression gate failed. The full
Leeds/XYZ demo remains unverified. Passing subsets below are historical
evidence, not acceptance of OMA-76. See the OMA-76 review findings in Linear;
the local reproduction ledger is `/tmp/oma76-independent/REVIEW.md`.

Baseline source: `47d373770b92dd929b82fe3bbc26b18ce07abc22`.
No deployment, production database, source data, or live workspace was changed.

## Design and capacity

Introspection uses one joined token/grant/client snapshot. It still queries the
database on every introspection and retains the grant-owning client check for
token B. Exchange, request binding, single-use consumption and approval predicates
retain their existing authority. No new authorization cache was added.

OAuth and config API each use one process-wide pool of three control sessions.
All store instances share that pool. With the deployed single process per
service, six sessions leave two of the existing eight-role budget for diagnostics
and administration. Additional replicas/workers require repartitioning this
budget before deployment. This does not provide admission across arbitrary
externally launched processes. Runtime-reader capacity remains eight, alongside
the forty XYZ live/preview sessions and two reserved diagnostic sessions.
Derived and semantic role limits remain unchanged.

Control acquisition has twelve waiter slots and a two-second deadline. Runtime
reader admission waits at most five seconds, optional generation reads at most
two seconds; their shared waiter budget is 32. Request deadlines can shorten
these waits. Expired or disconnected waiting requests do not enter the database
operation. The existing semantic-service timeout is unchanged. Running writes
retain conservative indeterminate-outcome reporting; cancellation is not a
promise that an already-started write was undone.

Leases roll back unfinished transactions, restore psycopg transaction defaults,
and run `DISCARD ALL` before reuse. Reset failures close the connection. Automatic
prepared statements are disabled because DISCARD removes server-side prepared
state. The identical module copies are needed by the separate Docker build
contexts and are guarded by a byte-for-byte packaging test.

## Measurements

[Raw results](auth-admission-results.json) retain per-call latency and errors.
These are small local fixtures, not statistically representative benchmarks or
measurements of historical deployment slowness. PostgreSQL 18.6, Python 3.14,
and psycopg 3.3.4 were used; production images use Python 3.12.

The probe runs the registered `capabilities_list` tool, token-A introspection,
identity resolution, exchange, config API authentication/redemption and the
actual capabilities route over real local HTTP. It does not exercise MCP wire
transport, XYZ, Docker networking or source-data queries. The baseline loads the
original SQL store, config store and introspection modules under the same probe
and transport wrappers, avoiding a second checkout.

| Path | Before connections / statements | After connections / statements | Before / after errors |
| --- | --- | --- | --- |
| Cold, one call | 19 / 41 | 2 / 63 | 0 / 0 |
| Warm, one call | 14 / 30 | 0 / 50 | 0 / 0 |
| Eight concurrent calls | 106 / 226 | 4 / 400 | 1 / 0 |

The full warm baseline includes one route-specific connection beyond the older
13-connection pre-route trace. Cold includes token-A introspection and identity
cache misses. Pools are closed after fixture seeding and before the cold probe.
Statement counts include setup/reset; reuse reduces connection creation while
safe reset increases statements. The eight-call after run accumulated 0.274 s
of queue time across all workers, separately from each call's total latency.

A final 32-call run produced 26 successes and six bounded
`auth.exchange_unavailable` overload errors; maximum whole-call duration was
1.254 s. Aggregate queue time was 2.395 s and control lease execution time
2.128 s across concurrent workers. These aggregates are not wall-clock latency.
Cold and warm execution time and queue time are also recorded in the raw file.

## Reproduction and checks

Use a new disposable UTF-8 PostgreSQL database for each probe, with an empty
`control` schema owned by a non-superuser login with `CONNECTION LIMIT 8`.
Set `CONTROL_TEST_DATABASE_URL` only to that disposable fixture. The probe
creates fixture records and refuses a database with existing OAuth clients;
it does not delete control tables. The existing SQL regression fixtures do
delete tables and must use a separate disposable database.

```sh
python scripts/auth_admission_probe.py --disposable --baseline
# Point CONTROL_TEST_DATABASE_URL at another fresh disposable database:
python scripts/auth_admission_probe.py --disposable
# Use another fresh disposable database:
python scripts/auth_admission_probe.py --disposable --concurrency 32

PYTHONPATH=mcp-auth:mcp-auth/tests python -m unittest discover -s mcp-auth/tests
PYTHONPATH=mapp-mcp:mapp-mcp/tests python -m unittest discover -s mapp-mcp/tests
PYTHONPATH=config-ui python -m unittest discover -s config-ui/tests
PYTHONPATH=config-ui:mcp-auth:mcp-auth/tests python -m unittest discover -s mcp-auth/tests -p test_sql_store.py
PYTHONPATH=mcp-auth python -m unittest discover -s mcp-auth/tests -p test_control_connections.py
PYTHONPATH=config-ui python -m unittest discover -s config-ui/tests -p test_control_plane.py
git diff --check
```

The checks used `/tmp/oma76-venv/bin/python`, with the repository requirements
installed into that temporary environment. The isolated PostgreSQL cluster used
a private Unix socket, no TCP listener, and no production credentials.

Verified: OAuth suite 510 tests (102 skipped); MCP suite 460 (3 skipped);
SQL-store suite 36; config control-store suite 25; pool integration suite six;
focused introspection suite 39; token-binding suite 57; deadline diagnostics five;
config suite 1290 (362 skipped).
Pool integration includes two separate service processes plus two diagnostic
connections, failed-connect recovery, session reset, transaction failure,
cancelled and expired waiters, saturation, and recovery. Optional database suites skipped in
broad discovery were run separately where listed above.

No image build, `mapp verify`, deployment, backup or restore verification was
performed. Those are not implied by the isolated checks.
