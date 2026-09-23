# Disposable derived relations: lifecycle and blast radius

This lifecycle removes persistent preview relations after an explicitly bound
workspace proposal is declined, or after the retention period approved at
creation expires. It is opt-in. Existing relations and proposals are not
backfilled or inferred to be disposable.

## Lifecycle

1. Plan and create with `draft: {expiresInHours: 24, cleanupApproved: true}`.
   Retention is an integer from 1 to 168 hours. Creation approval includes this
   policy and its future dependency-checked deletion.
2. Include the returned `{name, assetId, generation}` in `draftRelations` when
   checking and creating the workspace proposal. These bindings participate in
   the check fingerprint. Only the creator or an administrator may bind them.
3. Preview and review the proposal. A pending proposal keeps its drafts until
   their approved expiry; silence is not a rejection.
4. Publishing the relation makes it permanent. Explicitly declining the owning
   proposal makes an unpublished draft eligible for cleanup. Refusing an apply
   approval prompt does not decline the proposal; its retention deadline still
   applies.
5. Inspect `GET /api/derived-layers/drafts`, or MCP `derived_layers_drafts`, for
   adoption, deletion, and retention reasons. A decline response means the
   rejection was recorded, not that the database relation has already gone.

The MCP plan/create tools accept the same `draft` object or
`draft_expires_in_hours` as an equivalent convenience parameter; use one form
consistently across plan and create. Proposal bindings use `draft_relations`.
MCP `proposals_decline` records a final rejection through the
existing proposal decline API. See the [API contract](api-contract.md) and
[derived-layer guide](derived-layers.md) for the complete request contracts.

## Deletion boundary

Automatic cleanup is restricted to an active journal entry with the exact
managed name, immutable asset ID, and generation that were approved. Reusing a
name never transfers deletion rights. Ordinary source relations and permanent
derived relations are outside this boundary.

The worker checks the live workspace and other pending, applying, or conflicted
proposals, including their original and candidate workspaces. It validates
stored workspace hashes and inspects named locales, table mappings, and static
SQL references. Dynamic references and provider-backed query templates whose
SQL cannot be inspected retain drafts conservatively.

Shared filesystem locks protect proposal writes, workspace publication, and
preview dispatch. Cleanup takes an exclusive, nonblocking lock and defers if
those operations are active. A persisted global browser lease retains drafts
for at least 210 seconds after each runner dispatch, covering its maximum
180-second run and shutdown even if the requesting process loses the response.
Queued visual operations also block cleanup.

Publication protection covers direct dashboard saves and other workspace
publication paths, as well as applying the owning proposal. Durable publication
intent records bridge the workspace file and database transactions. A committed
publication is recovered as permanent adoption. An ambiguous intent retains the
relation for reconciliation, even if it no longer appears in the live map.

Finally, PostgreSQL rechecks identity and dependencies under the existing
mutation lock. Deletion uses `DROP ... RESTRICT`, never `CASCADE`. The drop,
definition removal, lifecycle update, and semantic archive outbox event commit
together. Failures roll back and remain eligible for a later retry. Applying or
conflicted owner proposals, missing ownership evidence, and identity drift
retain their relations.

## Operational impact

| Area | Change and boundary |
| --- | --- |
| Existing data | No enrollment, backfill, or automatic deletion of pre-existing relations. Permanent creation remains the default. |
| Database schema | Additive private `derived_layers._drafts` journal and active-cleanup index. Initialization is idempotent; journal history survives relation deletion. |
| Proposal API | Optional closed identity bindings in check/create. Existing requests without bindings retain their fingerprint behavior. |
| MCP authority | Two additional tools: draft inspection uses `inspect`; proposal decline uses `propose`. Automatic deletion relies on the cleanup policy approved during creation. |
| Workspace publication | Saves referencing managed relations resolve active draft identities. Saves publishing drafts also write recovery metadata and adopt them in the database. Failure before publication blocks the save; uncertain post-publication adoption retains recovery evidence. |
| Draft editing | Active disposable drafts cannot be replaced or refreshed. Publish them first or create a separately approved draft. Permanent relations retain their existing behavior. |
| Background load | One worker polls every 60 seconds, also wakes on decline, and considers at most 20 active drafts per pass in least-recently-checked order. Database mutation contention defers cleanup; relation lock waits are limited to five seconds. |
| Filesystem | Private control-state lock, browser lease, and publication recovery files; no new public mounts or XYZ access to control state. |
| Query safety | Existing source-profile, SQL-shape, H3, recursive plan, materialization-size, and database resource guards remain required. |

Each operational evidence scan stops conservatively at 1,000 records, 32 MiB
of aggregate state, or a two-second scan deadline; individual state files are
limited to 16 MiB. Missing, corrupt, symlinked, unknown, or oversized evidence
blocks deletion. These limits bound work rather than establish a cleanup SLA:
long-running previews, contention, large proposal histories, or dependencies can
keep a draft beyond its expiry. The inspection listing returns at most 100
records and indicates possible truncation.

Unregistered external SQL readers and unsaved client editor state cannot be
enumerated. Disposable relations must not be treated as permanent shared data.
Publishing adopts them permanently, including after subsequent map removal;
later removal requires the existing explicit, dependency-checked drop workflow.

## Rollout and recovery

Rebuild the configuration service, MCP server, and MCP authorization service
together so tool descriptors and envelopes agree. The journal is initialized
through the existing derived-store initialization path. No separate cleanup
service or deployment mount is required. A rollback to older code stops this
automatic cleanup and leaves the extra journal in place; retain the database
and control-state evidence for a later compatible rollout.

Back up and restore the database and private control state coherently. A missing
owner proposal or unresolved publication intent requires operator reconciliation;
do not delete evidence to force cleanup. Inspect the exact asset identity, live
workspace, proposal history, dependencies, and retained reason before using the
existing explicit drop workflow. Cleanup history itself is retained for audit.

Local validation covers PostgreSQL create/drop transactions, rollback and
dependency protection, identity reuse, coordinator recovery, cross-process
locking, HTTP proposal routes, MCP approvals/contracts, and existing query
guards. It does not constitute a deployed XYZ browser or end-to-end rollout
check. Disposable relations still persist in PostgreSQL: truly non-mutating
query-backed map rendering remains unsupported.
