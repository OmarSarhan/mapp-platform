# OMA-117 proposed request and mutation contract

**Proposed for parent review. OMA-76 is NOT READY.** This specification concerns
implementation `f3d8a921` (original baseline `47d37377`). The independent review
observed the OAuth token count rise from one to two after origin disconnect.
A reload with a 400 ms budget spent its receipt before lock release and then
published generation one. A chained SQL insert completed at 245 ms on a 180 ms
budget. These are baseline observations. The state transitions and acceptance
checks below are work orders.

## One operational budget

In `mapp-mcp/app.py:build_app`, keep metadata and era rejection ahead of
authentication. At entry to `ProtocolEraGuard.__call__` for an RPC POST,
**before the first body read**, create a per-request context beside
`CURRENT_CALLER`. Its minimal interface is:

| Operation | Contract |
| --- | --- |
| `start`, `deadline` | Local monotonic seconds. Initially allow the longest served operation (currently 120 s), shortened by any valid caller cap. Bound incomplete bodies too. |
| `tighten(operation_seconds)` | Set the bound to the earlier of the existing deadline and **start + operation_seconds**. Use the server's registered tool descriptor (default 15 s). Resolve the name from the already bounded body where unambiguous, before token-A I/O. Recheck against actual SDK dispatch. An untrusted name can only shorten the cap and grants no authority. Expiry at resolution refuses dispatch. |
| `remaining(cap)` | Observe cancellation and return the smaller of the local cap and the positive remainder, or raise a typed refusal. Check before *and after* waits, before dispatch, and at each effect gate. Never round an exhausted interval up into permission to start. A short hop limit applies to its entire call. |
| `cancel(reason)`, `arm(abort) -> disarm` | One latched, thread-safe signal per request. Registration races with cancellation under a lock. A late registrant must abort before use. Wake all active registrations, including concurrent children. Detach callbacks under the lock, execute outside it, and join in-flight aborts before resource reuse. A registration owns one resource lifetime. |

Copy the context **reference** through the SDK and worker boundaries. Separate
concurrent RPCs, including two on the same handshake session. Child calls may
shorten their own limit. A short identity or claim call must never enlarge or
overwrite its parent's limit. No new introspection cache.

For internal HTTP, retain `X-Mapp-Timeout-Seconds`. Compute it immediately
before send from the remaining budget. The receiver intersects the duration
with its local cap at entry. Absence uses the service default (config 120 s,
OAuth 10 s). A supplied nonfinite or malformed bound must not buy extra time.
The sender keeps its own absolute timer through
connect, write, response headers and the final body byte. Relative seconds do
allow transit time to be counted again at the receiver. For the single-host
Compose service hops, the preferred strict cutoff is an additional internal
monotonic expiry, used **only to shorten** both local bounds after confirming
that the services share a clock domain. Strip/ignore externally supplied
absolute values. OMA-118 must test delayed delivery. A cross-host monotonic
timestamp has no such meaning. Parent decision is required if these service
hops must support independent clocks with a strict simultaneous cutoff.

Human decision time belongs to `runtime.py:approval_gate` and
`_await_decision`. Keep their existing elicitation and approval lifetime.
Exclude only the time actually waiting for the human (including poll sleeps)
from the operational allowance. Suspend the operational timer only with no
outstanding operational I/O. Charge each create, poll/claim, confirm and final
spend to the remaining operational allowance, with its own hop cap. Keep origin
cancellation live throughout. Decline, disconnect or an expired approval
cannot open a fresh operational window. Already accepted durable background
jobs retain their own execution limits and status/cancel interface.

## Receive, workers and active HTTP

* **One raw reader.** Today `era_guard._buffer` assembles up to 512 KiB and
  replays once, then delegates to raw receive. In the pinned MCP 2.2.0 SDK,
  `StreamableHTTPSessionManager.asgi_app` wraps dispatch in
  `RequestBodyLimitMiddleware`, which buffers and replays again. Both transport
  eras read `Request.body()`. The modern streaming path also awaits receive in
  `handle_modern_request.watch_disconnect`. Give the guard sole ownership of
  raw receive: buffer to completion or fail on disconnect/expiry mid-body.
  After the last chunk, hand raw receive exclusively to one scoped watcher
  **before authentication**. Inner receive supplies the original body once,
  then waits for the latched real disconnect. Preserve accepted versions, body
  limits, SDK streaming and error responses. Stop and join the watcher on
  response completion or error. Cancel and account for any unadmitted work on
  scope exit, even if the SDK's session task outlives the POST. ASGI receive
  after response completion can report disconnect on a healthy connection.
  Finish the request at the final
  send and distinguish that event from abandonment.
* **Before and inside submission.** `BearerAuthentication.__call__` uses
  `asyncio.to_thread`. The SDK's `FuncMetadata.call_fn` sends sync tools
  through AnyIO's limiter, while `runtime.io_call` submits functions for async
  tools with shielded `run_sync`. An abandoned await leaves a running thread.
  Prefer a checked async wrapper at `build_runtime.tool` for sync registrations
  (preserve signatures/schemas), and the same checked submission at auth and
  `io_call`. Cancel queued admission on expiry, check again at worker entry
  and after CPU, and retain ownership of capacity until the worker exits.
  Session streams carry context at send time in the pinned SDK. Verify the
  actual per-call context across both eras.
* **During a child call.** `IntrospectionClient._post`,
  `ExchangeClient.instance_id/_post`, `ConfigApiClient._send` and
  `config-ui/mcp_token_client.py:McpTokenClient._post` use synchronous urllib.
  Waiting for `urlopen` to return provides no socket handle during connect or
  response-header wait. Prefer a request-owned HTTP(S) connection in these
  clients. Register a shutdown handle when the socket is created, before
  connect/send, and retain it until response reading ends. An abort during
  resolution prevents subsequent connect/send. Bound resolver concurrency,
  connect and streamed reads. Preserve
  TLS verification, destinations, redirect restrictions and credential scrubbing.
  Closing a response object alone cannot interrupt all these phases.
* **At the next service.** `AuthHandler._dispatch` and config
  `Handler.do_GET/do_POST` install `control_connections.request_scope`.
  Extend that scope with the same local interface. Observe immediate peer
  closure concurrently while the handler is inside child HTTP, a lock, or SQL.
  Example: while config waits for OAuth redemption, its upstream peer may
  close. Config must interrupt the OAuth socket even if its own worker is
  blocked in a read. Use non-consuming poll/peek, latch confirmed closure,
  distinguish unread input and inconclusive observations, and end observation
  before keep-alive reuse. `remaining_time()` at checkout alone misses this
  case. Each service owns its own signal. Cancel handlers must run even when
  a sibling's abort blocks. The wire conveys cancellation by
  shutting down the active hop. A sender cannot infer that a remote write
  was stopped from the successful shutdown of its socket. The ordering guarantee
  is at the receiver: observed cancellation wins against still-queued work.
  Remote submission racing an as-yet unobserved disconnect is indeterminate.

## Lease and command boundaries

OMA-119 owns the execution path through both identical `control_connections.py`
copies, `SqlStore._connect`, `ControlStore._db`, `control_schema.migrate`,
`runtime_database.dbs_connection` and both connection branches of
`semantic_sources._read_postgres_generation_context`.

1. Bound waiter admission, slot, connect and setup by one acquisition expiry,
   itself limited by the request. Preserve the nested-lease guard, three
   control slots and twelve waiters per service, eight DBS reader slots, the
   three-read generation limit, diagnostic headroom and semantic-service limits.
   Acquire the existing migration mutex with remaining time. Bound its advisory
   lock and whole migration transaction. Set `_migrated` only on confirmed
   success. The 30 s migration setting is a ceiling.
2. A synchronous psycopg `connect_timeout` cannot express the 50 ms reader
   case (the installed driver enforces a two-second minimum). Prefer interruptible
   connect under the same slot. If a bounded connector must finish after the
   caller returns, assign ownership of the slot and eventual close to that
   connector, keep *all* such workers counted and prove bounded reclamation.
   Recheck after connect/setup and before yield. A late connection goes directly
   to close with zero borrower SQL. Do not return it to idle via normal reset.
3. Give the borrower a checked lease exposing the existing connection and
   cursor APIs. Gate actual command submission for connection **and cursor** execution,
   explicit SQL BEGIN/COMMIT, driver commit and context-exit commit. Preserve
   transaction mode and the reader's first `SET TRANSACTION ... READ ONLY`.
   Bound active commands by the absolute expiry and confirmed disconnect,
   including SET/setup, lock waits and commit. Cap existing statement/lock
   limits by the remainder where legal. A deadline interrupt independent of
   borrower SQL must remain armed across chained statements and caller GUC
   changes. Recheck after password hashing and before the autocommit INSERT
   in `ControlStore.login`. An expired uncommitted transaction rolls back.
4. Lease owner serializes arm/disarm with command entry and release. On cancel,
   forbid further borrower sends, interrupt the active command and drain its
   result. A cancel can race just before send or after completion. Cover the
   former with server bounds and a coordinated command-start check. Prefer
   **evicting any connection for which a wire cancel was attempted**. A timer
   cancellation or even a successful `cancel_safe()` alone proves neither
   server cancellation nor safe reuse. Join/disarm watchers before rollback
   and reset. On an untouched healthy lease retain rollback, restored driver
   defaults and `DISCARD ALL`. Bound cleanup separately, close/evict on
   uncertainty and release once. Cleanup may settle an existing command, never
   submit a new application effect. Verify bounded cancel support on the
   deployed libpq version (psycopg falls back to blocking cancel on older ones).
5. OMA-121 may replace a dead *idle* backend found during setup once, under
   the same occupied slot and acquisition expiry, before yield. A failed
   replacement yields typed unavailability. After yield the borrower is invoked
   at most once, including on commit-ack loss.

## Authority, effect and response

Keep two facts per request: which one-shot authority has been spent (bound
token B, approval receipt, or spend uncertain), and the outcome of the **target
operation**. A token mint, login insert and approval claim are themselves
effects for their respective callers. Do not infer the target outcome from the
HTTP verb, the CSRF flag or the broker manifest's `mutating` bit.

| Transition | Evidence and permitted action |
| --- | --- |
| Queued/validating → refused before effect | Gate closed before target submission. For capacity/expiry, a fresh exact request is manually retryable only when the receiver proves no target effect and required authority is obtainable. A spent B requires fresh exchange. A surviving approved receipt still needs to be valid and held by the caller. |
| Authority consumed → refused before effect | Receipt spend committed but target gate closed. Report receipt spent, target unstarted, `retryable=false`, `indeterminate=false` if proven. Inspect approval/operation and target, obtain new explicit authority if still wanted. Never unconsume or reissue the receipt. Unknown spend requires inspection too. |
| Effect started → completed or rolled back | Mark started at the last serialized check immediately before submitting the first effectful command/publication. Acknowledged commit, authoritative target observation or confirmed rollback determines what completed. Report partial facts, e.g. workspace revision committed, reload completion unknown. A rollback covers only its own transaction. |
| Possible submission → indeterminate | Missing reply, interrupted commit, crash or failure to establish which target changes persisted. `retryable=false`, `indeterminate=true`, include authorized recovery references. Never replay application work automatically. A pending or accepted job is inspected by its existing operation ID. |

OMA-114 must move the receipt spend **together with** a mandatory effect gate.
Prefer two request-local methods on the config handler: `admit_authority()`
validates/spends the pending receipt once under the requisite locks, and
`begin_effect()` requires successful authority admission, checks the signal
and deadline, and advances the phase before calling the effect site. Carry the
bound gate into helpers that own inner locks. A required gate missing at a
guarded route fails closed. Browser/admin routes retain their own authentication
and CSRF gates and use the same deadline and effect accounting.

1. In `Handler._authorized/_redeem_exchanged_token` keep auth, scope, receipt
   presence, server-resolved route, canonical method/path/query/body/instance
   digest and authoritative token-B redemption. For approved routes carry a
   private pending gate bound to that exact request. Perform any early receipt
   eligibility check read-only. Until the gate wins, allow only private,
   side-effect-free preflight and ordinary refusal. Prevent protected output,
   background submission, proposal changes and target writes via every branch.
2. Acquire the locks needed for the **first effect**, preserving existing lock
   order and reentrancy. Use timed acquisition or nonblocking flock/recheck
   for lifecycle, PROPOSAL, SAVE_RELOAD, SAVE, RELOAD, `.request.lock` and
   `ControlStore._locked` where used. Revalidate revision/fingerprint inside
   the locks. Move the direct reload/apply `create_operation` out of the
   pre-receipt path: it writes a running record and can prune old records.
   For direct reload, give `request_reload` a gate at the point
   where both inner locks are held. Preserve metadata-before-generation
   publication. Provision lock files as coordination state or prove their
   creation cannot alter target state. Do not hoist reload locks across apply.
3. Recheck signal/time, perform the existing single conditional UPDATE in
   `ControlStore.redeem_receipt` (receipt hash, exact digest, approved,
   unrevoked, decision within lifetime, single winner). Bind recovery to that
   spend. An SQL success followed by an audit exception still spends the
   receipt. **Recheck again after** its SQL/audit/operation waits, immediately
   before the target effect. Once the first write starts, finish the smallest
   consistent publication or roll back. Bound subsequent waits and report partial progress.
   Apply begins at the first persisted proposal transition, including
   `applying`, before workspace replacement. `save_workspace` can write
   publication intent and backup before replace. The no-save mutate branch
   and `proposals.check` return a preview. Approval create, confirm and winning
   claim write even though their broker descriptors allow repeated redemption.
4. Prefer existing records. Link the validated request ID, public approval
   reference and exact operation/target to the winning spend durably **at the
   conditional update**, before independent file audit can fail. Reuse the
   existing operation target for that correlation, proposal ID/original
   revision/candidate hash or reload fingerprint/generation as available.
   Distinguish the operation kind from the durable operation instance ID.
   `read_approval_by_reference`, `read_operation`, proposal state and reload
   status support authorized inspection. A request ID in a lost response or
   an unlinked log is insufficient. OMA-114 must demonstrate lookup from a
   reference known before send, including a crash between DB spend and file
   creation. Review the smallest persistence change if the row cannot carry
   that link. Keep receipt, handle and credentials private. A caller that
   lost its one-time claim response cannot retrieve the receipt by ID.

OMA-120 catches typed admission **inside** config GET/POST request scopes and
OAuth `_route`, ahead of catalog/POST generic errors and the stdlib handler's
`TimeoutError` catch. A proven pre-effect busy response uses the documented
JSON 503, `database.busy`, `Retry-After: 1`, request ID and local queue/DB
timing. Include retry advice only when the authority predicate above holds.
OAuth uses `temporarily_unavailable`. Preserve the outcome fields and safe
references through config/broker clients and `runtime.spend`. Infrastructure
500/503 and budget-driven SQL cancellation leave credential validity unjudged.
Keep genuine binding refusals and indistinguishable inactive-token responses.

## Acceptance gates by owner

Use barrier-controlled entry/release and observations from a separate fixture
connection. Hold each blocker until refusal or cancellation is observed. Set a
finite test watchdog and publish a scheduler tolerance separately from the
request budget (proposed 100 ms on the idle wire fixture). No tolerance allows
a late effect. Each owner ships its failing-before regression.

| Owner | Test and required observation |
| --- | --- |
| 118, origin | Real Unix-socket MCP call, real OAuth/config. Warm token-A path, occupy three OAuth leases, observe exchange waiter, close origin. **Waiter clears while all three are still held**. Release, drain workers, compare minted-token count and target to before, then succeed on a fresh call. Repeat during cold introspection, config→broker wait, SDK tool-worker and auth-worker admission. Assert zero next-stage calls and no leaked worker/permit. |
| 118, time | Drip a chunked body, expire or disconnect mid-body, require zero introspections. Positive case replays exactly once through both SDK eras with no false disconnect. Gate auth/queue/cold identity/exchange so each duration fits but the sum crosses the tool cap. Record decreasing wire budgets and prove the next stage never starts. Cover connect/header/trickling-body waits and overlapping child calls. A second RPC on the session survives. Human wait longer than an ordinary hop still allows a timely authorized spend. |
| 119, late work | Hold password verification past the 20 ms login budget, then return a valid result. No session INSERT or success cookie. Hold a real reader connector past 50 ms, assert timely refusal while held, then release and see close, zero borrower SQL, restored capacity. Exercise control connect likewise. Three short SQL statements sharing 180 ms cannot reach a later INSERT. Cover both cursor/connection APIs, autocommit, explicit and implicit commit, and a lost commit acknowledgement. |
| 119, active waits | Hold a generation table lock across a 100 ms statistics request. Require refusal and backend exit from the lock wait **before unlock**, no result/provider call, then successful read. Repeat with first-use migration mutex and advisory lock. No committed partial ledger. Race cancellation with command completion and release using a paused callback. No next borrower on that backend until disarmed, evict on attempted wire cancel, next borrower completes its own query. |
| 114, authority | Hold SAVE_RELOAD, RELOAD and the file flock individually on a fresh approved reload. Exercise expiry and real peer closure. Before and after release verify receipt approved, no operation admitted, both metadata and requested generation unchanged. Fresh exact exchange against the surviving receipt succeeds once. Race two exact B tokens on one receipt, require one winner. Keep missing, mismatched body/path/revision, expired, revoked and replay refusals. |
| 114 + 120, outcome | Place barriers before apply writes `applying`, after receipt UPDATE before audit/first target write, and after workspace replace before acknowledgement. Inspect durable records and both resources, including on simulated process loss. No target write in the middle case, no second replacement in the last. Check HTTP and MCP diagnostics against those facts. Include login, approvals.create/claim and mutate save/no-save to detect verb-based classification. |
| 120, wire | Saturate identity, catalog and authenticated read-only POST using the real stdlib handler. Require one complete 503 with headers, correlation and timing, then successful recovery. Lock OAuth introspection and force exchange/redemption broker 500. Verify typed unavailability, genuine mismatch refusal, grant-owner revocation and identical inactive bodies across wrong audience, unknown and disabled. |
| 121, reuse | Healthy fd ≥1100, true EOF, and inconclusive poll tested separately. Terminate a disposable idle backend, verify one in-slot pre-yield replacement or bounded refusal. Force reset failure and failure after yield independently. Count borrower entries, fresh successes, original slot limits and clean session state. Check both module copies and packaging inputs. |

## Handoff

OMA-122 can repair fixtures independently. Retain injectable `connect` seams,
the nested-control refusal and transaction-private schema assertions on their
own connection. Exercise six service leases plus two diagnostics under the
dedicated capped role. Budget tests must use the runner's repaired fixtures
and preflight disposable targets before any migration/seed.

After parent review, start 118 with the two-era receive/worker/socket spike
and 119 with the subsecond connect and cancel/disarm tests. These have the
highest feasibility uncertainty. 121 can take the peer probe and pre-yield
replacement in parallel once 119 fixes the lease interface and assigns
ownership of their shared file. Start 114 after those gates, deciding the
DB/file correlation and lock order with 120 before changing receipt placement.
120 can establish real-handler failing tests immediately. Re-estimate after
these tests run. Independent wire and deployment gates remain with integration.
