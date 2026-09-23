# Changing a MAPP workspace

Read this before proposing or applying anything. It is the order the platform
expects, and each step exists because skipping it produces a change nobody can
review or undo.

## The order

**1. Establish the target.** `describe_instance` gives the instance identity,
the workspace key and the current revision. `capabilities_list` gives the
action schemas the server will actually accept. Treat those schemas as the
truth; do not infer that an action exists because it would be reasonable.

Stop on an instance-identity mismatch or an unsupported contract. Never assume
a familiar name still points at the server you think it does.

**2. Inspect before proposing.** Use the smallest set of reads that answers the
request. `layers_list` to find a layer key, `layers_get` for one layer whole,
`catalog_list` to resolve which columns exist, `layers_values` and
`layers_statistics` for distribution. `dependencies_list` tells you what else
reads a relation, which is the question to ask before altering anything shared.

A displayed field is not necessarily a column. `layers_values` needs a real
column of the layer's table, which `catalog_list` resolves.

**3. Build the smallest operation set.** One coherent change. Do not bundle
unrelated edits into a proposal a person then has to accept or reject whole.

**4. Check, then propose.** `proposals_check` validates operations against a
revision and returns a `check_fingerprint`; `proposals_create` takes that
fingerprint. The fingerprint is what binds the proposal to what you checked —
if the workspace moved in between, the create is refused rather than silently
applied to a different starting point.

Both take `operations` and a `revision`, not a candidate document. You never
send a whole workspace.

For a disposable derived preview, first obtain approval for creation with
`draft={"expiresInHours": 24, "cleanupApproved": true}` or
`draft_expires_in_hours=24` as described in `mapp://guidance/derived-layers`.
Pass its exact `name`, `assetId`, and `generation` in `draft_relations` to both check
and create. The fingerprint binds ownership as well as workspace changes.
Publication retains the relation; decline or its approved expiry schedules
guarded cleanup. `derived_layers_drafts` reports the cleanup outcome.

**5. Show the change before asking for it.** `proposals_show` gives the diff a
reviewer reads. `proposals_preview_plan` describes the intended view;
`proposals_preview_test` and `proposals_preview_screenshot` render the proposed
state through a real browser and attach the result to the proposal. A proposal with evidence is worth far
more than one without, because the person deciding can see it rather than
imagine it.

Screenshot and test previews return immediately with an `operationId`, including
when `hover: true`. Poll `visual_operations_show` after `pollAfterSeconds` until
the status is terminal; each poll uses a fresh credential and the render keeps
running independently. Use `artifacts_image` with a returned PNG artifact path
to show the retained screenshot in chat. A running preview is not a pass.

For a choropleth overview, use `framing: "layer"` to fit the full effective
filtered layer, or `framing: "viewport"` to preserve supplied `centre` and
`zoom`. Without those coordinates, viewport framing uses the configured locale
startup view; it cannot read an unsaved browser viewport. The default
`framing: "feature"` remains useful for close-up hover and feature-information
checks. Area-wide screenshots can pass while a centre interaction finds no
feature, so inspect the separate interaction evidence.

**6. Apply only when a person agrees.** `proposals_apply` asks, in the session,
and does nothing until answered. Pass `evidence_operation_id` from a preview
run so the person sees the render alongside the diff.

Applying is not reversible by an undo. Recovering means proposing the inverse
change.

**7. Verify.** The apply result tells you two separate things: whether the
workspace was written, and whether the tile service picked it up. They can
differ. If `mapReloaded` is false the change *is* applied — do not apply again.
Check `xyz_status`, and use `xyz_reload` if it is still serving the old
workspace.

## What you will be asked to confirm

Seven operations ask a person before they happen: applying a workspace or
semantic proposal, reloading the map, and creating, replacing, refreshing or
dropping a derived layer. The prompt is rendered by the person's own client,
not by you, and the summary comes from the platform's record of the change.

A refusal after you asked is a decision, not an error to work around. Do not
retry a declined action with different wording.

When the user rejects the proposal itself, `proposals_decline` records that
final decision. Its owned disposable derived relations then become eligible
for cleanup under the policy approved at creation. Refusing an apply approval
alone does not decline the proposal; its drafts remain until the approved
retention expiry unless it is explicitly declined or published. Report cleanup
status from `derived_layers_drafts`; rejection does not prove deletion finished.

## Non-negotiable

- Never edit or upload a workspace document directly. The proposal tools are
  the only route.
- Never apply without a separate, explicit approval for that specific change.
- Never silently rebase a stale proposal. If the revision moved, say so and
  compose again from the current one.
- Never resend an operation whose outcome was reported indeterminate. Inspect
  first — `operations_show` follows the asynchronous half.
- Never assume ambiguous layer, locale, style-state or SQL intent. Ask.
- Never treat a truncated result as evidence of a distribution.
- Never expose a token or other secret.

## When something is refused

Read the refusal. The platform names the rule it applied and, for a validation
failure, the field and what the database said about it. `rules` lists the
authoring rules with their remediation; `schema` gives the shape a workspace
must take.

A refusal naming a scope means the grant does not carry it. That is an
operator's decision to change, not something to route around.
