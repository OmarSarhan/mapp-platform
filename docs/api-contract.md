# Configuration API contract

The configuration API is the integration boundary between MAPP Platform and
the separately released `mapp-config-cli`. The platform is authoritative for
workspace structure, rules, validation, revision handling, proposals, reloads,
and visual evidence.

## Discovery and versioning

`GET /api/public/identity` is public and returns the instance identifier,
authentication mode, contract version, and pinned XYZ version.

After bearer or dashboard-session authentication:

- `GET /api/contract` returns API, contract, rules, and XYZ versions, supported
  commands, workflow, and exit-code meanings.
- `GET /api/schema` returns the server workspace schema; a JSON Pointer may
  select a subsection.
- `GET /api/rules` returns validation and remediation rules, optionally by
  category.
- `GET /api/examples` returns server-owned examples.
- `GET /api/layers?locale=KEY` returns the server-composed effective layer map
  and exact workspace revision. Omitting `locale` selects the top-level XYZ
  default; the CLI does not reproduce XYZ merge rules locally. Clients must
  require the advertised `layers effective` capability before using this
  endpoint so they fail closed against an older independently released server.

The current API and contract versions are `1.0`; the rules version is `1.3`.
A formal compatibility policy is still required. The CLI should reject an
unsupported major contract version and should not assume that a newer command
exists merely because an older server used it.

Schema, rules, and examples are also the CLI-facing source of truth for layer
ordering. `group` is navigation-only; clients must use numeric `zIndex` values
for stable drawing order (higher values render above lower values), or
`promoteDisplay` for the dynamic “move above displayed layers when shown”
behavior. The `setLayerDrawingOrder` example demonstrates revision-bound
operations without requiring the CLI to encode XYZ semantics locally.
The `showLayerLegend`, `setCategorizedSymbology`,
`showSymbolInFeatureInformation`,
`countLayerInViewport`, and `showViewportCountBesideLayer` examples publish
the optional operations for a basic layer legend, a clicked-feature geometry
swatch/icon, a Filtering-panel count, and a bracketed layer-heading count.

Request bodies must be JSON objects of at most 5 MiB. Parsing is strict:
`NaN`, `Infinity`, and `-Infinity` are not JSON values and are rejected.
Responses are also emitted as strict JSON.

## CLI operations for symbology, information swatches, and viewport counts

The standalone CLI continues to use revision-bound proposal operations; this
repository does not contain a second CLI implementation. Inspect the effective
layer and current revision before constructing either optional change:

```sh
config-cli workspace get
config-cli layers get 'Bus Stops'
```

Create and review a basic legend proposal:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/style/theme={"type":"basic","label":"Bus stop","style":{"icon":{"url":"/instance/svg/bus.svg","scale":1}}}' \
  --set '/locale/layers/Bus Stops/style/elements=["theme"]' \
  --explanation 'Shows the existing Bus Stops symbol in the optional XYZ Styling-panel legend.'
```

Create and review categorized, data-driven symbology. Category `value` entries
must exactly match the stored field values. When `style.elements` already
exists, inspect it and retain unrelated enabled controls rather than copying
this intentionally minimal example:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/style/theme={"type":"categorized","title":"Bus stops by town","field":"town","categories":[{"value":"Leeds","label":"Leeds","style":{"icon":{"type":"dot","fillColor":"#176b4d","scale":1}}},{"value":"Wetherby","label":"Wetherby","style":{"icon":{"type":"dot","fillColor":"#277da1","scale":1}}}]}' \
  --set '/locale/layers/Bus Stops/style/elements=["theme"]' \
  --explanation 'Uses exact town values for the Bus Stops symbols and XYZ legend.'
```

The dashboard’s richer feature-information preview is a review surface, not a
separate workspace property. CLI clients configure the same backend
`style.theme` object and should use a bounded `visual-test` after application
to verify the rendered categories and legend.

Create and review a viewport-count proposal. The `infoj` array index must come
from the inspected revision; this example's `2` is the Object ID entry in the
seed workspace:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/filter={"viewport":true,"includeAll":false,"count_meta":"features currently visible"}' \
  --set '/locale/layers/Bus Stops/infoj/2/filter=true' \
  --explanation 'Adds an optional Filtering-panel count scoped to the current viewport.'
```

To show the viewport count directly beside the layer name, preserve any
existing entries in `plugins` when constructing the inspected revision's new
array:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/plugins=["/instance/plugins/viewport-layer-count.mjs"]' \
  --set '/locale/layers/Bus Stops/viewport_layer_count={}' \
  --set '/locale/layers/Bus Stops/filter/viewport=true' \
  --explanation 'Shows the current viewport count in brackets beside the Bus Stops layer name.'
```

This heading badge does not require an interactive `infoj` filter. It respects
one when present, only queries while the layer is visible, and is removed by
deleting `viewport_layer_count` and the corresponding plugin URL. Do not remove
unrelated URLs from an existing `plugins` array.

`filter.viewport_description` is deliberately absent from these guided
examples. Pinned XYZ v4.23.4 preserves the value but leaves its generated
element hidden, so the dashboard and CLI contract do not advertise it as a
visible effect.

Create and review a clicked-feature symbol proposal. The null fill and stroke
values prevent XYZ's selected-location palette from adding another swatch
behind a point icon:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/infoj/0/style={"fillColor":null,"strokeColor":null,"icon":{"url":"/instance/svg/bus.svg","scale":1}}' \
  --set '/locale/layers/Bus Stops/infoj/0/_dashboard={"styleFromLayerDefault":true}' \
  --explanation 'Shows the existing Bus Stops icon in clicked-feature information and marks it for dashboard synchronization.'
```

Use the returned focused diff and evidence in the normal `proposals create`,
explicit approval, `proposals apply --confirm`, reload-status, and visual-test
workflow. To remove either optional feature, create a new revision-bound
proposal using `--unset` for `style.theme` (and remove only its `theme` element
when appropriate), or for `filter.viewport`, custom count text, and the entry
filter. Remove the bracketed layer-heading count by unsetting
`viewport_layer_count` and removing only
`/instance/plugins/viewport-layer-count.mjs` from the inspected layer's plugin
array. Remove a dashboard-managed feature-information symbol by unsetting the
geometry entry's `style` and ownership marker. Do not apply the original
request as approval.

## Core reads

| Route | Purpose |
| --- | --- |
| `GET /api/workspace` | Workspace plus bytes-and-file-generation revision |
| `GET /api/layers?locale=KEY` | Server-composed effective layers for the selected locale |
| `GET /api/catalog` | Database connections and renderable tables visible to XYZ |
| `GET /api/derived-layers/capabilities` | Managed-view and H3 availability |
| `GET /api/derived-layers` | Managed derived-layer definitions |
| `GET /api/derived-layers/<name>` | One definition including its SQL |
| `GET /api/icons` | Valid public SVG choices |
| `GET /api/sql/capabilities` | Supported calculated-value expression model |
| `GET /api/proposals` | Proposal summaries |
| `GET /api/proposals/<id>` | Complete proposal record |
| `GET /api/xyz/status` | Requested/applied reload generations and health |
| `GET /api/artifacts/<path>` | Authenticated visual report or image |
| `GET /api/auth/me` | Current actor and reported scopes; session list for administrators |
| `GET /api/capabilities` | Stable action IDs, risks, routes, schemas, and operation kinds |
| `GET /api/operations/<id>` | Durable authorized status/result for a long action |

## Mutations

Managed derived-layer database actions are separate from workspace proposals:

- `POST /api/derived-layers` creates a dependency-checked view or materialized
  view with the `derive` scope.
- `POST /api/derived-layers/<name>/refresh` refreshes a materialized view only
  with `{"confirmed": true}`.
- `POST /api/derived-layers/<name>/replace` atomically validates and swaps a
  complete definition, including kind conversion, with explicit confirmation.
  Results include `columnChanges`, `workspaceReferences`, `fieldReferences`,
  and `requiresSecondOrderChanges`; blocked dependency errors also identify
  `removedColumns` and `dependentColumns`. Dashboard and CLI clients should
  show `userMessage` followed by `suggestedAction`; JSON paths, PostgreSQL
  object descriptions, and `technicalDetail` are diagnostic fields and should
  not be the primary user notification.
- `POST /api/derived-layers/<name>/drop` removes a managed object only with
  `{"confirmed": true}`. Replacement and drop report detected PostgreSQL
  dependents; drop also reports live workspace references and refuses removal.

Creating a derived relation never adds it to the workspace or reloads XYZ.
That remains a separately reviewed, revision-bound workspace proposal.

Managed changes use a list of operations:

```json
[
  {
    "op": "set",
    "path": "/locale/layers/Bus Stops/display",
    "value": false
  }
]
```

Paths use strict RFC 6901 JSON Pointer escaping: `~0` represents `~` and `~1`
represents `/`; any other tilde escape is invalid. Object keys and zero-based
array indices are supported, but leading-zero, negative, missing, and
out-of-range array indices are rejected. `set` adds or replaces a value and
may append at the array length; `unset` removes an existing value. Replacing
or deleting the workspace root (the empty pointer) is not supported. The
pointer `/` is distinct: it addresses an object member whose key is empty.

Important routes include:

| Route | Purpose |
| --- | --- |
| `POST /api/validate` | Validate a complete candidate workspace |
| `POST /api/mutate` | Validate operations; save only when explicitly requested |
| `POST /api/proposals/check` | Preflight a revision-bound operation set without creating a proposal |
| `POST /api/proposals` | Create a revision-bound proposal lifecycle record |
| `POST /api/workspace` | Validate and save a complete workspace, then wait for its fingerprint-matched reload |

Proposal visual requests accept `viewMode` as `focus` (the default) or
`default`. Focus mode activates the requested layer and relevant group context
through XYZ's `layers` query parameter. Default mode omits that parameter for
both original and candidate renders so initial `layer.display` changes can be
verified without the preview overriding them.
| `POST /api/proposals/<id>/apply` | Apply a pending proposal, then wait for its fingerprint-matched reload |
| `POST /api/proposals/<id>/decline` | Record rejection and optional reason |
| `POST /api/sql/test` | Probe one calculated information expression |
| `POST /api/visual-plan` | Choose a data-aware view, with optional named `locale` and bounded `centre`/`zoom` override |
| `POST /api/visual-test` | Run browser validation and create before/after artifacts, with the same locale and view overrides |
| `POST /api/xyz/reload` | Request a generation-based XYZ reload and wait for TCP readiness with the current workspace fingerprint |

Administrator-session routes create/revoke bearer tokens and change the
administrator password.

The managed write routes return HTTP 200 only after the XYZ supervisor reports
child-process TCP readiness with the exact committed workspace fingerprint.
`POST /api/xyz/reload` is the explicit operator/recovery endpoint; when no
fingerprint is supplied, it derives and binds the current live workspace
fingerprint before requesting the reload. A supplied fingerprint that is no
longer current returns `409 workspace.fingerprint_conflict` without restarting
XYZ. A successful normal workspace save or proposal apply does not need a
second reload request.

The optional request field is `workspaceFingerprint`, containing a lowercase
64-character SHA-256 digest. The server validates it against the current live
workspace while holding the save/reload lock.

## Revisions and proposals

The revision is derived from the exact current workspace bytes and file
generation. A proposal records the original revision and applies only if that
revision is still current. A stale apply returns a conflict; clients must
inspect the new state and create a new proposal rather than silently rebasing.

Proposal records include complete original and candidate workspaces as well as
operations, focused diff, explanation, warnings, actor, timestamps, hashes,
and lifecycle status. Original intent and candidate content are retained while
the server updates status and apply/decline metadata. These are sensitive
operational records, not append-only files.

Application persists an internal `applying` transition before replacing the
workspace. If the service exits after the exact candidate commits, a repeated
apply of the same already-approved proposal can reconcile the candidate hash,
finish the `applied` record, and request reload again. If the workspace differs
from both the original revision and candidate, the proposal becomes
`conflicted` and must not be silently rebased. Clients may therefore observe
`pending`, `applying`, `applied`, `declined`, or `conflicted`; only the exact
approved proposal is eligible for recovery.

`POST /api/proposals/check` accepts the same `revision`, `operations`, and
optional explanation as proposal creation. It runs the same candidate-building,
workspace validation, and bounded SQL probes, but neither saves the workspace
nor creates a proposal lifecycle record. A successful response contains a
`check` object with `valid: true`, `proposalCreated: false`, original revision
and hash, candidate hash, operations, focused diff, explanation, and warnings.
It also returns `checkFingerprint`, a SHA-256 binding of the revision,
candidate hash, and exact operations. Clients may send that fingerprint with
the unchanged cached operation set to `POST /api/proposals`. The server
recomputes it and rejects a mismatch with `proposal.check_fingerprint`.
Proposal creation remains an independent authoritative validation and can
still conflict if the live revision changes between requests.

Candidate visual evidence is proposal-bound:

| Endpoint | Behaviour |
|---|---|
| `POST /api/proposals/{id}/visual-plan` | Plan a candidate view, using the retained original only when the requested layer was deleted; advertised to clients as `proposals preview-plan` |
| `POST /api/proposals/{id}/visual-test` | Render and test the candidate in isolated XYZ; advertised to clients as `proposals preview-test` |
| `POST /api/proposals/{id}/screenshot` | Render a high-resolution original-versus-candidate comparison; advertised to clients as `proposals preview-screenshot` |

Each response reports `source: "candidate"`, `proposalId`, and `candidateHash`.
Visual-plan and visual-test browser evidence preserves that binding. A
screenshot comparison additionally binds its nested before report to
`source: "original"` and `originalHash`, while the after report remains bound
to the candidate. Only a pending, integrity-valid proposal whose original
revision is still current is eligible; declined, applied, conflicted, corrupt,
or superseded proposals are rejected. The request accepts visual `layer`,
`locale`, bounded centre/zoom overrides, and viewport fields. It never accepts
an arbitrary workspace.

Proposal screenshots default to a square 1080×1080 viewport at 1× device scale
and capture only that viewport, producing an exact 1080×1080 page image.
Request overrides are bounded to a 320–2560 pixel width, 240–1440 pixel height,
and a 1×–3× device scale. The browser report's `capture` object records the
effective viewport, scale, capture mode, and actual PNG dimensions so reviewers
can verify the retained evidence resolution.

For probeable database layers, visual planning focuses a representative
feature near the layer extent centre and records an `interaction` plan for the
browser runner. A proposal screenshot publishes the retained original and then
the candidate to the same isolated preview process and renders both at the
same view. Its `beforePage`/`beforeMap` artifacts therefore represent the
original proposal revision, while `afterPage`/`afterMap` represent the
candidate—not merely pre-click and post-click candidate states.

When the focused diff changes the selected layer's `infoj` feature-information
configuration, the runner selects the same representative feature in both
renders, waits for XYZ's expanded `.location-view` panel to finish loading,
and uses those selected states for the before/after page images. It also
returns cropped `beforeInfoPanel` and `afterInfoPanel` artifacts. For other
proposal changes the comparison remains unselected so a point highlight does
not hide a symbol-style change. The separate visual-test endpoint continues
to exercise the planned feature interaction.

When a proposal adds, removes, or moves the requested layer in an XYZ `group`,
the comparison isolates that layer: an addition is off before and alone after,
a removal is alone before and off after, and a move keeps only the moved layer
on in both renders. Other group members remain available in XYZ navigation but
are not activated. Ordinary property edits that leave membership unchanged
retain the active group as visual context. A deleted requested layer is planned
from the original; its candidate side intentionally has no proposal layer
active. The response plan reports `changeKind`, `groups`, candidate `layers`,
`anchorLayer`, `requestedLayerDeleted`, and `viewSource`; nested browser reports
record each side's actual activated layers and verify that its remaining group
drawers are present.

Candidate rendering uses a dedicated XYZ process, workspace path, and reload
mailbox. Preview requests are serialized from candidate publication through
browser completion, so one candidate cannot replace another mid-render. This
process never writes, replaces, or reloads the live workspace or live XYZ
process.

For an `infoj[].fieldfx` operation, the service derives the effective locale,
layer relation, information-field renderer, and expected result type from the
workspace when these values are unambiguous. Validation errors may include that
safe metadata so a client can construct a structured `sql.test` next action. It
must not echo an SQL expression, database URL, credential, authorization header,
or sensitive sample value in diagnostics or remediation data. If discovery is
ambiguous, the preflight returns a diagnostic instead of guessing.

## Locale composition

The top-level `locale` is XYZ's default rendered locale, including when a
`locales` object is also present. XYZ pre-composes the top-level default into
each named locale except a named key literally called `locale`; that name
resolves the top-level default and is not a distinct alternative. If no locale
is requested, layer reads, SQL tests, and visual planning use the top-level
default. An explicit alternative name selects its effective composed locale.
If raw `workspace.locale` is absent, XYZ synthesizes the default as
`{"layers": {}}`. Both an omitted locale and the explicit name `locale`
select that empty default; the platform never auto-selects a sole named
alternative. Named alternatives other than `locale` compose over the
synthetic base.

Composition follows XYZ's special nested merge rules. Objects merge by key.
Arrays concatenate unless all source items are already present, in which case
the source array replaces the target; scalar values follow XYZ's source/target
precedence. Comma-separated locale composition uses the same framework rules.
The platform must not substitute a generic recursive merge. Mutations always
target the raw workspace JSON, so a focused operation must identify whether a
value is inherited from the default or owned by the named locale override.
Creating a real default where XYZ previously synthesized one requires adding
the raw `/locale` property explicitly.

Templates, external renderers, inline features, and zoom-keyed table or
geometry mappings are preserved and schema-validated. They are not forced
through a concrete database-relation probe when no single relation represents
their effective source.

## Authentication and device authorization

Legacy `full` bearer tokens remain compatible. Device credentials support
`inspect`, `propose`, `visual`, `apply`, and `reload`, enforced server-side.
The default agent request is `inspect + propose + visual`; apply and reload
must be requested explicitly. Direct workspace writes and administrator routes
remain full/administrator-only.

`POST /api/auth/device` creates a rate-bounded ten-minute authorization and
returns an opaque device ID plus user code. An administrator reviews the exact
device name and scopes. `POST /api/auth/device/token` returns the thirty-day
token once; subsequent polls return `consumed`. Raw device IDs and tokens are
not written to audit records. Approval stores no raw credential; the token is
created only on the first approved poll, and only its hash is persisted
atomically with the consumed authorization state.

## Response metadata and operations

JSON responses include `meta.requestId` and `X-Request-ID`. Responses tied to
a durable operation also include `meta.operationId` and an operation record.
States are `running`, `succeeded`, `failed`, and `indeterminate`. Visual tests,
proposal applies, and explicit reloads retain bounded mode-`0600` records;
reading one requires its corresponding visual, apply, or reload scope.

Indeterminate is not a retry instruction. Proposal apply retains the existing
committed-state recovery rules, while reload recovery inspects XYZ generation
and fingerprint state first. Unexpected reload/apply exceptions after an
operation begins are recorded as `indeterminate`; any operation still
`running` when the configuration service starts is also transitioned to
`indeterminate` with `operation.interrupted`.

## Error handling

Clients should preserve the server's structured JSON error and use stable
non-zero exit codes for usage, validation, revision conflict, connectivity,
visual, and authentication failures. They must not print bearer tokens,
authorization headers, database URLs, or sensitive SQL samples.

Visual results include a structured diagnosis with independent HTTP, canvas,
layer-binding, and page-error checks, the selected viewport/view, and a bounded
failure class.

HTTP `422` validation errors from preflight and proposal creation use a common
diagnostic shape:

```json
{
  "error": "Validation failed.",
  "errors": [
    {
      "ruleId": "sql.result_type",
      "pointer": "/locale/layers/Example/infoj/0/fieldfx",
      "path": "locale.layers.Example.infoj[0].fieldfx",
      "phase": "sql",
      "severity": "error",
      "message": "Expression returns text; bool is required.",
      "expectedType": "bool",
      "actualType": "text",
      "locale": "locale",
      "layer": "Example",
      "field": "enabled"
    }
  ]
}
```

`ruleId`, `pointer`, `phase`, `severity`, and `message` provide stable
machine-readable context; type and SQL-discovery fields are included when
relevant. Errors block proposal creation. Successful checks and proposals may
contain review warnings; informational observations are non-blocking. Clients
should render these categories separately and may derive `nextActions` with
structured action IDs and arguments rather than shell command strings.

Important status semantics are:

| Status | Meaning |
| ---: | --- |
| `400` | Malformed/oversized JSON object, invalid JSON Pointer, or invalid request value |
| `409` | Revision, proposal lifecycle, or integrity conflict |
| `422` | Candidate validation or browser validation did not pass |
| `429` | Login or visual-runner concurrency limit reached; retry later |
| `504` | The workspace/proposal write may already be committed, but XYZ reload confirmation did not complete |

A browser-validation `422` retains the selected plan and the browser runner's
failed report and artifact paths. Clients should surface that evidence rather
than replace it with a generic error.

The browser runner allows one active test by default and is hard-clamped to
1–4. When it is full, `/api/visual-test` propagates HTTP 429 with the selected
plan and retryable error instead of converting the condition to an upstream
failure.

For direct workspace or saved-mutation responses, `saved: true` in a `504`
means the new revision and fingerprint were committed. For proposal apply, an
`applied` proposal with `appliedRevision` is the committed-state signal. Do not
blindly retry either result: read the proposal, workspace revision, and XYZ
status first. Dry-run mutation must return `saved: false`; a client should fail
closed if a server does not make that guarantee.

## Compatibility testing

Every platform/CLI release pair should test discovery, schema/rules, workspace
reads, dry-run mutation, validation failure, proposal lifecycle, stale
revision, reload status, visual results, authentication failure, and redaction.
The client should use the server's contract rather than duplicating evolving
workspace rules.
