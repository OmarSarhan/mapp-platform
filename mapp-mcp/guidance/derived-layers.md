# Managed derived relations

Use a derived layer when one rendered relation must combine or spatially derive
data from source relations. These act on the database directly, without a
database proposal or review queue. Creation and explicit mutation ask a person
first. Disposable draft creation
can also authorize guarded automatic cleanup under the recorded policy.

Requires `derive:manage`, the widest scope an operator can grant.

## Views or materialized

An ordinary **view** tracks its sources: it recomputes on read and needs no
refresh. A **materialized** relation is faster to read and stale until
`derived_layers_refresh` runs. Choose materialized only when the work is
expensive enough to be worth the staleness, and say which you chose and why.

The default is a view, because it is the cheap always-correct choice.

## Naming

Normalize the relation name before proposing it: `^[a-z][a-z0-9_]{0,62}$` — a
lowercase ASCII letter first, then lowercase letters, digits and underscores,
up to PostgreSQL's 63-byte identifier limit. The same rule applies to the ID
and geometry column names you select.

Do not invent spaces, hyphens, dots, uppercase or quoted mixed-case
identifiers. The server fixes the output schema to `derived_layers` and quotes
accepted identifiers itself.

## Plan before you create

For a request to preview before making any change, explain that rendering still
requires approved database creation. Planning is non-mutating; rendering a new
derived result is not. Disposable drafts reduce retained clutter but create a
real relation that other database connections can read. A preview request alone
is not approval for creation or its automatic cleanup policy.

`derived_layers_plan` runs the probes the platform would run, reports what
would happen, and returns a `plan_fingerprint`. Pass that fingerprint to
`derived_layers_create` and the create is refused if the sources moved in
between.

This matters more here than for a workspace proposal: a derived layer leaves no
proposal behind, so the plan is the only description of the change that exists
before the change does.

The plan leaves no persistent relation. It does not execute the output query,
compute observed quintiles, or render a map. Its estimates are not evidence of
the actual numeric distribution. After approved creation, use the bounded
aggregate-only numeric inspection tools on the effective layer dataset to
choose and verify category breaks.

## Disposable relations for proposal previews

For an explicitly disposable preview, pass `draft_expires_in_hours` (an integer
from 1 to 168) to both `derived_layers_plan` and `derived_layers_create`. Keep
that value identical when using the plan fingerprint. The creation prompt
authorizes the real database change and dependency-checked automatic deletion
after its proposal is declined or retention expires. Without this parameter,
creation keeps the existing permanent lifecycle.

Pass the created relation's exact `name`, stable `assetId`, and `generation` as
one `draft_relations` binding in both `proposals_check` and `proposals_create`.
Those identities are bound into the check fingerprint. The candidate must use
the relation. Never infer ownership from names, age, or the fact that a relation
is absent from the live map, and never attach a pre-existing permanent relation
to obtain automatic deletion rights.

Applying the workspace proposal publishes and retains its disposable relations.
Declining its proposal makes them eligible for cleanup after active previews
finish. Abandoned drafts become eligible only at their explicitly approved
expiry. Live workspace use, other pending proposals, PostgreSQL dependencies,
and active previews block deletion. Cleanup rechecks identity and generation,
uses no cascading drop, records its result, and retries deferred work.

Use `derived_layers_drafts` to inspect ownership, retention, and cleanup results.
When the user rejects the preview, call `proposals_decline` for the owning
proposal. Refusing an apply approval only refuses that attempt; it does not
decline the proposal or trigger immediate cleanup. The draft still has its
approved retention deadline. Never infer a final rejection from silence.

A pending proposal is not itself authorization to delete an ordinary relation;
existing relations and proposals are not retroactively enrolled. A blocked
cleanup is an operational result to report, not permission to force a drop.

## Spatial scope is fixed at creation

Every create and replace resolves a fixed spatial scope from the selected
locale's configured extent. It selects whole output features intersecting that
envelope — it does not clip geometry, and it does not follow later pan, zoom or
workspace-view changes. `derived_layers_map_extent` reports what will be used.

A replace resolves and saves the scope again. A refresh does not.

**The outer intersection guard filters final output rows only.** It is not a
security boundary, and it does not map-scope upstream aggregates, windows,
limits or computation. When the metric itself must be map-scoped, put the
envelope predicate in the source-side SQL *before* aggregation — which also
avoids an unbounded upstream calculation.

## Replacing is the quiet one

A drop announces itself: the platform refuses it while anything still reads the
relation. A replace does not. Every layer reading it keeps working and starts
returning different numbers.

So check `dependencies_list` before proposing a replace, and say in your
summary what will change underneath. The approval prompt carries the dependent
list, but the person should not be meeting it for the first time there.

## Dropping

The one genuinely destructive tool on this surface.

- **Name the relation exactly.** No pattern, no "the one I just made", nothing
  inferred from earlier conversation.
- **It is refused while anything depends on it**, by name. If a layer still
  reads the relation, remove that layer first — which is its own proposal and
  its own approval.
- **The approval names what would break**, so a person sees the consequence
  rather than the verb.
- **It cannot be undone.** Recovering means rebuilding the relation.

## Refresh costs real work

A refresh reads every source row again. It asks a person every time, and that
is not ceremony — it is the one operation here an agent could usefully repeat,
and the database would be the thing paying for it.

Pass `background=true` for a long one and follow it with `operations_show`.
