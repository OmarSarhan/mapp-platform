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

The current API and contract versions are `1.6`; the rules version is `1.6`.
The machine-readable compatibility and pagination declaration is versioned at
[`contracts/api-compatibility-v1.6.json`](../contracts/api-compatibility-v1.6.json).
The CLI rejects an unsupported major contract version and does not assume that
a newer command exists merely because an older server used it.

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

### Bounded collection pages

Growing collection routes advertise pagination contract `1`. A bounded
request supplies `limit` (1–100, default 100) and may supply the opaque
`cursor` returned as `pagination.nextCursor` by the preceding request. A null
`nextCursor` is the only end-of-collection signal. Keep all filters unchanged
between pages. A cursor that fails integrity checks or no longer matches its
declared revision, visibility, filters, or configuration scope returns
`pagination.invalid`; restart at the first page rather than interpreting or
modifying the cursor. Source-relation cursors do not bind live PostgreSQL
catalog contents or grants; those follow the keyset consistency behavior below.

Every collection response has a 16 MiB ceiling. Item arrays use a 15 MiB
budget, reserving response headroom for pagination and diagnostic fields and
remaining below the configuration service's 20 MiB semantic-upstream guard. A
bounded response may therefore contain fewer items than the requested `limit`
while returning a non-null `nextCursor`; clients must not infer completion
from a short page. If one item cannot fit, the gateway returns HTTP `413` with
`semantic.page_too_large` for private-semantic collections or
`pagination.page_too_large` for local collections. The requested bound remains
in `pagination.limit`, and the supported range remains 1–100.

Paged semantic catalog, search, history, derived-profile, source-relation, and
proposal reads use ordered storage keysets and fetch at most `limit + 1` rows.
The local derived-profile cursor is additionally bound to the semantic catalog
revision and whether administrator delivery diagnostics are visible. A
source-relation cursor is bound through a keyed digest to the platform instance
and effective source connections, allowlist, and exclusions; connection
credentials are not exposed as a public verifier. Eligibility is applied in
PostgreSQL before its ordered keyset limit, and discovery keeps no more than
`limit + 1` relation summaries in memory across aliases. It is not a
cross-request catalog snapshot: a relation created after the boundary may
appear on a later page, one created before it may require a fresh traversal,
and a dropped relation disappears. Filesystem-backed workspace proposal pages
parse at most `limit + 1` proposal documents; they scan proposal directory
names with bounded memory to preserve the established newest-first order.

Administrator derived-profile reads query at most one delivery failure per
displayed profile. Their first page also carries at most 100 unmatched archive
repair records in `deliveryBlockers`; `deliveryBlockersMore: true` means more
work remains. Repair the displayed blockers and refresh the first page for the
next batch. Continuation pages do not repeat the unmatched batch. Consumers
must treat `deliveryBlockersMore` as a JSON boolean: the dashboard acts only on
literal `true`, and the CLI fails closed if the flag is malformed or appears
without the accompanying blocker array.

API-major-1 parameterless reads retain the exact legacy response shape through
100 items. Above that threshold, or when the legacy shape would exceed the byte
ceiling, they fail with HTTP `409` and `pagination.required`; they never fully
materialize an unbounded collection. Parameterless search keeps its historical
20-result window while probing up to 101 matching rows to enforce that same
collection threshold. Contract-1.4-aware clients always send `limit`, keep one
page in memory, and expose `nextCursor` for an explicit user request. The
paginated routes and item fields are enumerated in the compatibility artifact.

Release the contract-1.4-aware CLI before enforcing the legacy threshold. Then
deploy semantic service 1.1.0 together with the matching `config-ui` image;
that image contains both the gateway and its bundled dashboard, so the
dashboard is not a separately deployable release step. The bundled dashboard
already sends `limit=100` and exposes manual continuation. An older browser
session that still makes parameterless requests remains compatible through 100
items and receives an actionable `pagination.required` error rather than a
partial result above that threshold; refresh it onto the bundled dashboard.

## CLI commands, actions, and scopes

`GET /api/contract` is the runtime authority for exact CLI command names.
`GET /api/capabilities` is the runtime authority for action IDs, risk classes,
routes, input schemas (including closed schemas where advertised), conditional
scopes, presentation hints, and durable operation kinds. A similar action ID
or matching route does not grant a missing command: the CLI fails closed unless
the connected contract advertises the exact command or compatibility marker
required by that invocation.

The non-semantic command families map as follows. A dash means that the route
is command-advertised but does not have a separate entry in `actions[]`;
clients still use the route and response contract owned by the server.

| CLI command family | API route(s) | Capability action ID(s) | Required scope |
| --- | --- | --- | --- |
| `setup`, `init`, `auth replace` | Public identity, then authenticated contract/connect; setup finishes with `describe` | —; client-side bootstrap/profile operations | Public identity is unauthenticated; a valid token can initialize/replace, while setup's final workspace check needs `inspect` |
| `profiles *`, `completion` | No API request | —; local-only commands | None |
| `doctor` | Public identity, contract/connect, and conditional workspace/semantic readiness reads | —; client-side diagnostic, intentionally not command-gated | Depends on the checks supported by the target and granted to the credential |
| `describe`, `schema`, `rules`, `examples`, `explain-error` | `/api/public/identity`, `/api/contract`, `/api/connect`, `/api/workspace`, `/api/schema`, `/api/rules`, `/api/examples` | — | Public identity is unauthenticated; `inspect` covers workspace and guidance reads |
| `capabilities list\|show` | `GET /api/capabilities` | Discovery response containing `actions[]` | Any authenticated credential, including a semantic-only token |
| `plugins list\|show\|validate\|usage` | `GET /api/plugins` | — | `inspect` |
| `workspace get`, `layers list\|get\|style-elements\|filters`, `catalog list`, `icons list`, `sql capabilities` | Workspace, layer, catalog, icon, and SQL capability GET routes | — | `inspect` |
| `layers values` | `GET /api/layers/{layerKey}/values` | `layers.values` | `derive` + `semantic:inspect`; returns bounded category counts from the effective layer restrictions, never raw rows |
| `layers statistics` | `GET /api/layers/{layerKey}/statistics` | `layers.statistics` | `derive` + `semantic:inspect`; returns bounded numeric aggregates, never raw rows |
| `validate` | `POST /api/validate` | — | Legacy `full` or administrator session; it never saves |
| `set`, `unset`, `amend` | `POST /api/mutate` with `save: false` | — | Legacy `full` or administrator session; the CLI rejects direct save |
| `sql test` | `POST /api/sql/test` | — | Legacy `full` or administrator session; read-only bounded probe |
| `derived-layers capabilities\|list\|show\|map-extent` | `GET /api/derived-layers/*` | `derived-layers.map-extent` for the extent preview | `inspect` |
| `derived-layers plan-area-weighted-h3` | `POST /api/derived-layers/recipes/area-weighted-h3/plan` | `derived-layers.plan-area-weighted-h3` | `derive` + `semantic:inspect`; returns a resolved, fully preflighted create request and applies no mutation |
| `derived-layers create\|refresh\|replace\|drop` | Managed derived-layer POST routes | `derived-layers.create`, `derived-layers.refresh`, `derived-layers.replace`, `derived-layers.drop` | `derive`; create/replace also require `semantic:inspect` for ready relation-source profiles |
| `proposals check\|create` | `POST /api/proposals/check`, `POST /api/proposals` | `proposals.check`, `proposals.create` | `propose` |
| `proposals list\|show` | Proposal GET routes | — | `inspect` |
| `proposals decline` | `POST /api/proposals/{proposalId}/decline` | — | `propose` |
| `proposals apply` | `POST /api/proposals/{proposalId}/apply` | `proposals.apply` | `apply` |
| `visual-plan`, `visual-test`, `screenshot` | `POST /api/visual-plan`, `POST /api/visual-test` | `visual.plan`, `visual.test`, `visual.screenshot` | `visual` |
| `proposals preview-plan\|preview-test\|preview-screenshot` | Proposal visual-plan, visual-test, and screenshot routes | `proposals.preview-plan`, `proposals.preview-test`, `proposals.preview-screenshot` | `visual` |
| `xyz status\|reload` (`reload-xyz` is a client alias) | `GET /api/xyz/status`, `POST /api/xyz/reload` | `xyz.reload` for reload | `inspect` or `reload` |
| `operations show\|wait` | `GET /api/operations/{operationId}` | Determined by the originating action's `operationKind` | The originating `visual`, `apply`, `reload`, or `derive` scope |
| `operations cancel` | `POST /api/operations/{operationId}/cancel` | `derived-layer.create`, `derived-layer.replace`, or `derived-layer.refresh` | `derive`; explicit confirmation required |
| `auth status\|device` | Auth identity and device-authorization routes | — | Any authenticated credential can read its identity; the CLI verifies the current target before using the unauthenticated device start/poll endpoints |
| `semantic *` | `/api/semantic/*` | `semantic.*` | See [Semantic catalog and proposals](#semantic-catalog-and-proposals) for the exact additive scopes |

Not every HTTP endpoint is a standalone CLI command. `/api/auth/login`,
`/api/auth/logout`, and the password, token, device-approval, and audit routes
under `/api/admin/*` are administrator dashboard-session surfaces. Direct
`POST /api/workspace` and a saving
`POST /api/mutate` are administrator/full-token compatibility surfaces; the
standalone CLI never uses them to save and supports only the revision-bound
proposal workflow. The CLI may use `/api/mutate` for `saved: false` dry-run
validation and uses `/api/sql/test`, not the legacy `/api/expression-test`
surface. The private semantic service, browser runner, preview publisher, and
XYZ reload channels are internal service interfaces and must never be called
by a remote CLI.

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

For point layers, XYZ can compose an icon array from multiple categorized
fields. In that shape, use `style.theme.fields` and give each category a
category-level `field`. Do not also set `style.theme.field`:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/style/theme={"type":"categorized","title":"Bus stop status markers","fields":["status","priority"],"categories":[{"field":"status","value":"open","label":"Open","style":{"icon":{"type":"dot","fillColor":"#176b4d"}}},{"field":"priority","value":"high","label":"High priority","style":{"icon":{"type":"triangle","fillColor":"#f8961e"}}}]}' \
  --set '/locale/layers/Bus Stops/style/elements=["theme"]' \
  --explanation 'Composes point icons from status and priority categories without setting a top-level theme field.'
```

The dashboard’s richer feature-information preview is a review surface, not a
separate workspace property. CLI clients configure the same backend
`style.theme` object and should use a bounded `visual-test` after application
to verify the rendered categories and legend.

Graduated themes require an actual numeric field and ordered unique numeric
breaks. For `less_than`, breaks ascend; for `greater_than`, they descend:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/style/theme={"type":"graduated","title":"Bus stops by score","field":"score","graduated_breaks":"less_than","categories":[{"value":10,"label":"Up to 10","style":{"icon":{"type":"dot","fillColor":"#a8d5ec"}}},{"value":50,"label":"Up to 50","style":{"icon":{"type":"dot","fillColor":"#277da1"}}}]}' \
  --explanation 'Uses ordered numeric score breaks for Bus Stops symbology.'
```

`GET /api/layers/{layerKey}/statistics` provides the bounded metadata needed
to review those breaks. It accepts one stored numeric `field`, `bins` from 1
through 50, and at most 20 repeated `threshold` and 20 strictly increasing
`break` values. The read-only five-second queries apply the effective layer's
fixed filter and identifier restrictions, exclude non-finite values from the
distribution, and return total/null/finite counts, min/max, fixed
0/25/50/75/100 discrete percentiles, histogram bins, threshold counts, and exclusive
upper-bound candidate class counts. Empty distributions return null min/max
and empty quantile/histogram arrays. Values are aggregates only; no source row
or field value is returned.

Use a raw numeric field for statistics, filtering, and symbology. A separately
formatted text field belongs only in hover and feature information. For a
one-decimal display, a threshold such as `0.05` can be audited directly; a
final `less_than` break and Filtering maximum should still sit one display
increment above the observed maximum when the UI boundary is exclusive.

Distributed themes require a stable identity field and at least one usable
style. XYZ reuses the palette and attempts to avoid equal styles on
intersecting features:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/style/theme={"type":"distributed","title":"Distributed bus stop palette","field":"object_id","categories":[{"label":"Green","style":{"icon":{"type":"dot","fillColor":"#176b4d"}}},{"label":"Blue","style":{"icon":{"type":"dot","fillColor":"#277da1"}}}]}' \
  --explanation 'Distributes a two-symbol palette using the stable Bus Stops feature ID.'
```

The API’s `fieldReferences` response includes direct theme fields, indexed
multi-field theme entries, and category-level fields. If a derived relation
replacement removes or changes any of them, CLI clients should present the
returned `userMessage` and `suggestedAction`, ask whether to replace the field,
change mode, refresh/reinspect the derived relation, or abandon the follow-on
proposal, and never choose a correction silently.

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
| `GET /api/catalog` | Database connections and renderable tables offered for new layers; omits the PostgreSQL `public` schema |
| `GET /api/derived-layers/capabilities` | Managed-view, executable H3-wrapper readiness, supported recipe availability, generated-row-aware nested-loop planning, and materialized-size guard availability |
| `GET /api/derived-layers/map-extent?locale=KEY` | Preview the selected effective locale's configured north/east/south/west extent, with the legacy view-derived fallback when those bounds are incomplete |
| `GET /api/derived-layers` | Managed derived-layer definitions |
| `GET /api/derived-layers/<name>` | One definition including its SQL |
| `GET /api/semantic/status` | Semantic schema version, catalog revision, and advertised capabilities, including Gemini context limits when configured |
| `GET /api/semantic/catalog?limit=N&cursor=CURSOR` | Export one bounded page of visible ready semantic assets at a catalog revision; archived assets are omitted even for administrators |
| `GET /api/semantic/catalog/search?q=TEXT&limit=N&cursor=CURSOR` | Search one bounded page of visible ready generated and curated metadata; archived assets are omitted |
| `GET /api/semantic/catalog/objects/<asset-id>` | Read one generated/curated asset profile; an archived asset requires an exact lookup with `semantic:inspect + semantic:admin` |
| `GET /api/semantic/catalog/objects/<asset-id>/history?limit=N&cursor=CURSOR` | Read one bounded chronological page of immutable snapshots for one visible asset; archived history requires an exact lookup with `semantic:inspect + semantic:admin` (`config-cli semantic catalog history ASSET_ID`) |
| `GET /api/semantic/derived-profiles?limit=N&cursor=CURSOR` | One bounded page of managed derived-layer semantic profiles and readiness |
| `GET /api/semantic/derived-profiles/<name>` | Read the semantic profile bound to one managed relation |
| `GET /api/semantic/proposals?limit=N&cursor=CURSOR` | One bounded page of semantic proposal summaries, optionally filtered by state or asset |
| `GET /api/semantic/proposals/<id>` | One semantic proposal and focused diff |
| `POST /api/semantic/generate` | Produce a review-only semantic draft for a table or stable field ID, with optional bounded data context |
| `GET /api/icons` | Valid public SVG choices |
| `GET /api/sql/capabilities` | Supported calculated-value expression model |

Derived-layer capabilities set `h3Available` only when the exact
extension-owned `h3_polygon_to_cells(geometry, integer)` overload passes the
same catalog policy used for submitted queries and successfully executes a
bounded synthetic polygon probe. `h3Readiness` reports
`method: "postgresql-catalog-and-execution"` and the corresponding `ready`
boolean; the probe reads no source relation or user row. Readiness requires
PostGIS 3.5.x plus matching H3 and H3 PostGIS 4.2.x versions. A false result
also has `code: "derived_layer.h3_not_ready"`, a closed failure `stage`, and
bounded `reasons` with `code`, `message`, and `suggestedAction`; it never
contains raw SQL, database errors, connection context, secrets, or arbitrary
catalog names. `h3Available` always equals `h3Readiness.ready`, and false H3
readiness does not disable derived queries that do not use H3.
| `GET /api/proposals?limit=N&cursor=CURSOR` | One bounded page of proposal summaries |
| `GET /api/proposals/<id>` | Complete proposal record |
| `GET /api/xyz/status` | Requested/applied reload generations and health |
| `GET /api/artifacts/<path>` | Authenticated visual report or image |
| `GET /api/connect` | Validate any bearer token and report its actor, granted scopes, token ID, and expiry without requiring an inspect scope |
| `GET /api/auth/me` | Current actor and reported scopes; session list for administrators |
| `GET /api/capabilities` | Stable action IDs, risks, routes, schemas, and operation kinds |
| `GET /api/operations/<id>` | Durable authorized status/result for a long action |
| `POST /api/operations/<id>/cancel` | Request cancellation of a background derived-layer transaction |

Derived-layer create, replace, and refresh requests accept an optional
`"background": true`. They return `202 Accepted` with `operation` and
`statusUrl`; poll that URL until `status` is `succeeded`, `failed`, `cancelled`,
or `indeterminate`. `cancelling` is nonterminal: the server reports `cancelled`
only after PostgreSQL confirms the transaction was rolled back. Omitting the
flag preserves the synchronous API behaviour.
The server advertises `backgroundJobs.activeJobs` and `maxActiveJobs` in
derived-layer capabilities. If the bounded worker is full, admission returns
HTTP `429` with `derived_layer.background_capacity`, `blocked: true`, and
`retryable: true`; it does not queue an unbounded thread or operation record.

## Semantic catalog and proposals

The semantic API is exposed through the authenticated configuration service;
clients never call the private semantic container. Profiles separate
source-owned `generated` facts from reviewed `curated` meaning. Generated facts
are read-only to user clients and are updated by idempotent derived-layer
lifecycle events.

Curated operations use strict JSON Pointer paths rooted at `/curated`. A
proposal check accepts an `assetId`, the current positive integer
`baseVersion`, one to 100 non-empty `set` or `unset` operations, and an
optional explanation. The explanation is part of the checked fingerprint. A
root `set` may replace `/curated` with a JSON object; the root cannot be unset.
Nested paths address object keys and use RFC 6901 `~0` and `~1` escaping.
The reserved `curated.fields` object is keyed only by active generated field
IDs. Field annotations and the complete map are size-bounded and are
revalidated inside apply's asset-version transaction.

The write workflow is:

| Route | Scope | Purpose |
| --- | --- | --- |
| `GET /api/semantic/source/relations` | `semantic:inspect` + `semantic:source` | List allowlisted PostgreSQL relations visible through the exact configured read-only database alias; no rows or column values are read |
| `POST /api/semantic/source/sync` | `semantic:inspect` + `semantic:source` | Register or refresh one exact relation from locked read-only catalog metadata; unchanged metadata is a catalog no-op |
| `POST /api/semantic/source/archive-excluded` | `semantic:inspect` + `semantic:admin` | With `{"confirmed": true}`, archive every ready PostgreSQL source profile matching the configured exclusions; database relations are unchanged |
| `POST /api/semantic/catalog/objects/<asset-id>/archive` | `semantic:inspect` + `semantic:admin` | With `{"confirmed": true}`, archive one ready semantic profile while retaining exact-ID administrator audit access and leaving database data unchanged |
| `POST /api/semantic/generate` | `semantic:inspect` + `semantic:generate`; add `semantic:data` when either context option is true | Send bounded authorized semantic metadata and optional 5% sample/statistics context to Gemini, then return targeted draft operations without retaining a proposal |
| `POST /api/semantic/proposals/check` | `semantic:propose` | Validate operations and return their focused diff and fingerprint without retaining a proposal |
| `POST /api/semantic/proposals` | `semantic:propose` | Create the exact fingerprinted, version-bound pending proposal |
| `POST /api/semantic/proposals/<id>/apply` | `semantic:apply` | Apply the pending proposal after explicit confirmation |
| `POST /api/semantic/proposals/<id>/decline` | `semantic:propose` | Confirm decline of the pending proposal with an optional reason |
| `POST /api/semantic/derived-profiles/<name>/repair` | `semantic:admin` | Explicitly requeue the retained derived event after its delivery failure has been investigated |

The generation request is closed: it contains `assetId`, a `target` of either
`{"kind":"table"}` or `{"kind":"field","fieldId":"..."}`, and an
optional `contextOptions` object whose only properties are boolean
`sampleRows` and `statistics`. Omitting it, or setting both values false, is
metadata-only. `sampleRows: true` selects from 5% of the relation and sends at
most 100 rows, 96 KiB, 20 eligible columns, and 512 characters per serialized
value; field generation includes only the selected field, and geometry/binary
values are omitted. `statistics: true` sends a planner estimate and column
counts for a table, or aggregates calculated over at most 1,000 rows from a 5%
field sample. Statistics do not disclose their contributing raw values.
`GET /api/semantic/status` is authoritative for availability and these caps.
The response reports the exact booleans in `generation.contextOptions` and
sets `generation.metadataOnly` accordingly. Neither optional context nor the
sample values are returned in the draft or stored in semantic history.

Applying succeeds only while the asset still has the checked `baseVersion`.
Source registration, replacement, refresh, or another curated apply increments
that version. A stale request returns `409`; clients must inspect and create a
new proposal instead of rebasing it.

Semantic proposal responses use `actor` for the proposal creator.
`decidedBy` and `decidedAt` are null while it is pending and are populated by
apply or decline with the decision actor and time. Migrated legacy decisions
may retain null decision fields where that evidence was not recorded.

The dashboard must fetch `GET /api/semantic/proposals/<id>` for the exact
stored pending proposal and render its explanation and focused diff before
enabling **Apply**. A proposal-list summary is not reviewed evidence. Apply
still requires a separate explicit confirmation after that review.

Asset-history reads return the asset ID, the catalog revision observed in the
same semantic-store snapshot, and chronological entries containing the source
event or proposal identity, actor, change type, version, generation, time, and
complete asset snapshot. The dashboard exposes this through **Immutable asset
history** on the selected semantic asset.

`schemaVersion` in semantic status identifies the semantic store/API data
shape. `catalogRevision` is the monotonically increasing revision of the whole
catalog snapshot; it is not an asset count and is distinct from an asset's
optimistic-locking `version`, its source `generation`, and the workspace
revision. Clients use the asset `version`, not `catalogRevision`, as
`baseVersion` for a curated semantic proposal.

Every semantic asset also carries `sourceState`. It is `null` while the
relation the asset was generated from is usable, and `"unavailable"` once the
platform has observed that it is not — a federated source that has been retired
or that verification can no longer reach. It is deliberately separate from
`status`: `archived` records an operator's confirmed decision, whereas
`sourceState` is an observation that reverses itself when the source returns,
and an archived asset whose source also disappeared carries both. The asset,
its `id`, and its curated semantics are retained throughout, so a source coming
back restores exactly its own annotations rather than requiring them to be
recreated.

`SEMANTIC_SOURCE_EXCLUSIONS` affects future source discovery and synchronization
but does not automatically hide profiles registered before the setting was
changed. The confirmed archive-excluded action performs that explicit
lifecycle transition. After either archive action, catalog, search, and
derived-profile collection reads omit the tombstone for all callers, including
administrators. Ordinary exact asset/history reads return `404`; callers with
both `semantic:inspect` and `semantic:admin` can still retrieve those exact
records by a previously retained asset ID. Archived assets cannot receive new
generation drafts, source events, or curated proposals, and removing an
exclusion does not unarchive them.

Removing only a reviewed annotation is not an archive operation. Check and
apply an `unset` below `/curated`, such as `/curated/description`,
`/curated/fields/<field-id>/description`, or the complete
`/curated/fields/<field-id>` annotation. This leaves generated relation/column
facts and the PostgreSQL data intact. A curated proposal cannot remove a
generated column; only a trusted source refresh can reflect that source-schema
change.

Derived relation creation stores a generated-profile outbox event in the same
PostgreSQL transaction. The `derive` scope is sufficient for that automatic
registration; it does not grant curated edit or apply rights. Profile states
reported by managed definitions are `registering`, `ready`, and
`repair_required`. A new workspace reference to the relation is rejected until
the current profile is `ready`; retaining an existing reference produces a
warning, while removing it remains allowed.

The configuration service validates each retained event envelope and payload
hash before dispatch and validates the semantic acknowledgement against the
event ID, payload hash, asset, generation, resulting status, and catalog
revision. Eligible events are atomically claimed in PostgreSQL with a unique
claim ID, an expiring lease, and `SKIP LOCKED`; only the matching claim can
commit a delivery result. Lease expiry recovers abandoned work, while stable
event identity makes a repeated dispatch idempotent. It delivers events in
order per asset and managed derived name; an undelivered earlier event blocks
every later generation or replacement asset for that name. The worker
automatically retries pending and retrying events.

The administrator route named `repair` only moves one retained
`repair_required` event back to pending for another delivery attempt. It does
not change the payload or resolve a deterministic 4xx, corrupt event, or
invalid acknowledgement, so those failures recur until their cause is
corrected. The request requires `{"confirmed": true}` and is unavailable unless
a retained `repair_required` event exists for that derived-layer name.

Derived-profile reads always obtain their top-level `catalogRevision` from the
live semantic service. They return `503 semantic.unavailable`, without a
fabricated revision, when that service cannot answer. An administrator session,
legacy `full` token, or `semantic:inspect + semantic:admin` token additionally
receives a name-level `delivery` object for the first blocker, containing its
event ID, operation, generation, status, attempts, and bounded single-line
error. Ordinary inspectors do not receive that diagnostic or raw outbox data.
On list responses, unmatched retained events for definitions already dropped
are returned separately as admin-only `deliveryBlockers`; they remain
repairable by their retained derived name. An archive retry reports
`pending_archive`.

Derived mutations and the confirmed retry are rejected while the bundled reset
maintenance gate is active. Synchronous requests receive HTTP `409` with code
`derived_layer.maintenance`, not malformed-input validation. A background
mutation already accepted with `202` instead finishes its operation record as
failed when it reaches the gate.

Asset IDs remain stable across the derived relation lifecycle. Field IDs remain
stable while a field name remains present across source generations. Dropped
assets are retained as archived tombstones, and curated annotations for
removed fields are retained as orphans instead of silently reassigned.

See [Semantic metadata control plane](semantic-layer.md) for the storage and
trust boundaries.

## Federated PostgreSQL sources

Available where `MAPP_DATABASE_MODE` is `bundled` or `federated`; the routes
return `federation.not_configured` otherwise. There are no CLI commands for
these and no dashboard UI — they are API-only. See
[Federated PostgreSQL sources](federation.md) for the operator procedure.

| Route | Capability action ID | Required scope |
| --- | --- | --- |
| `GET /api/federation/aliases` | `federation.aliases.list` | `federation:observe` |
| `GET /api/federation/aliases/{alias}` | `federation.aliases.show` | `federation:observe` |
| `POST /api/federation/aliases` | `federation.aliases.register` | `federation:register` |
| `POST /api/federation/aliases/{alias}/observe` | `federation.aliases.observe` | `federation:provision` |
| `POST /api/federation/aliases/{alias}/provision` | `federation.aliases.provision` | `federation:provision` |
| `POST /api/federation/aliases/{alias}/retire` | `federation.aliases.retire` | `federation:provision` |

Observe requires `federation:provision` rather than `federation:observe`
because it opens an outbound connection to a third-party database. The
`federation:*` scopes are peer to each other and non-hierarchical, and are not
reachable from any other scope.

The alias list is bounded by the 100-alias registry ceiling and returns one
response with no cursor; retired aliases are omitted from it while
`GET /api/federation/aliases/{alias}` still returns them by exact name, along
with their archive location and full observation history.

Each alias record carries `acceptedEvidenceComplete`. It is false for an alias
approved before the current accepted-evidence columns existed; such an alias
cannot satisfy the currency test and needs reprovisioning rather than waiting
for verification to fix it.

`POST .../provision` requires `expectedObservationId` and refuses when it does
not match the latest observation. Three conditions each need their own
explicit boolean — `acknowledge_row_level_security`,
`acknowledge_schema_change`, `acknowledge_physical_rebind` — and are refused
rather than assumed.

Federation errors use `federation.*` codes in the standard error shape. The
full list, with meanings, is in
[Federated PostgreSQL sources](federation.md#error-codes). Two are worth noting
here because they are retryable rather than terminal and are reported as `409`:
`federation.derived_layers_busy` and `federation.verification_in_progress`.

## Mutations

Managed derived-layer database actions are separate from workspace proposals:

- `POST /api/derived-layers/recipes/area-weighted-h3/plan` is the read-only
  exception in this POST family. It resolves a ready semantic polygon source,
  constructs the supported scope-bounded EPSG:27700 area-allocation query, and
  runs the exact create preflight. Its response includes
  `mutationApplied: false`, resolved source/field metadata, explicit allocation
  assumptions, a replayable selector-based `createRequest`, the full
  `resolvedSpatialScope`, and all applicable plan/size probes. Review that
  evidence, then submit the returned request separately to create; planning
  itself never creates a database object or workspace change. Create resolves
  the scope and preflights again so intervening workspace drift remains
  authoritative.
- `POST /api/derived-layers` creates a dependency-checked view or materialized
  view with `derive` and `semantic:inspect`; every declared relation source
  must have a ready semantic profile. Database functions, including H3 and
  PostGIS functions, are not relation sources and do not need their own
  semantic profiles. Source profile meaning is authoritative over an agent's
  guess; an agent should search/show the catalog first, then use authorized
  source discovery and synchronization only when the required profile is
  absent. The server always resolves and persists a fixed output-geometry
  intersection guard from the default effective locale. An optional
  `spatialScope: {"type": "workspace-map-extent", "locale": "..."}` selects a
  named locale instead. The fixed 1920×1080 area uses `max(0, view.z - 1)`.
  The derived relation `name`, `idColumn`, and `geometryColumn` must match
  `^[a-z][a-z0-9_]{0,62}$`: lowercase ASCII letters, digits, and underscores,
  starting with a letter and limited to 63 characters.
- `POST /api/derived-layers/<name>/refresh` refreshes a materialized view only
  with `{"confirmed": true}` and only after its computation and size probes
  pass.
- `POST /api/derived-layers/<name>/replace` atomically validates and swaps a
  complete definition, including kind conversion, with explicit confirmation
  and `semantic:inspect` for its ready semantic source profiles. Every
  replacement resolves a scope; omission selects the current default locale.
  Results include `columnChanges`, `workspaceReferences`, `fieldReferences`,
  and `requiresSecondOrderChanges`; blocked dependency errors also identify
  `removedColumns` and `dependentColumns`. Dashboard and CLI clients should
  show `userMessage` followed by `suggestedAction`; JSON paths, PostgreSQL
  object descriptions, and `technicalDetail` are diagnostic fields and should
  not be the primary user notification.
- `POST /api/derived-layers/<name>/drop` removes a managed object only with
  `{"confirmed": true}`. Replacement and drop report detected PostgreSQL
  dependents; drop also reports live workspace references and refuses removal.
- `GET /api/dependencies` lists database relations currently referenced by the
  effective workspace and managed derived-layer catalog. Supplying `alias`,
  `schema`, and `relation` together checks one relation and returns
  `blocked`, `matches`, and an operator-facing `message`. The route is
  read-only and does not discover external clients that only read from
  PostgreSQL.

Creating a derived relation never adds it to the workspace or reloads XYZ.
That remains a separately reviewed, revision-bound workspace proposal.
Map-extent preview is advisory; the canonical `spatialScope` returned by the
create/replace response is authoritative. The outer intersection guard keeps
complete intersecting output features but is not a security boundary and does
not push filtering below an aggregate. Layer-level aggregates and windows use
the complete submitted query input, never a size-probe sample, before final
output geometries are filtered. Agents must put the envelope inside source-side
SQL only when counts or metrics themselves are intentionally map-scoped.

Before any create or replacement, and before a materialized refresh, the server
runs non-writing PostgreSQL plan analysis over the exact scoped query and
recursively enforces advertised cost, row, intermediate-byte, join-expansion,
node, depth, and worker limits. SQL-shape and bounded H3-expansion checks run
before planning. `queryGuard` advertises its ordered AST/catalog/EXPLAIN
`stages`, `shapeLimits`, plan `limits`, H3 bounds, and `errorCategories`.
Schema-qualified PostGIS/H3 cast targets enter catalog validation only when the
type name is allowlisted; before query analysis, the server proves exact type
membership in the expected extension and requires the qualifier to match that
extension's authoritative namespace and the controlled `public` schema. The
selected output geometry must still carry an explicit geometry subtype and
positive-SRID typmod; a runtime SRID on a generic `geometry` column is
insufficient.
Configured routines remain prohibited except for SQL-language H3 PostGIS
polygon wrappers whose exact routine setting is the catalog-derived
`pg_catalog` plus authoritative allowlisted-extension namespace path. Both the
routine and implementation must resolve as members of an approved extension;
same-named custom routines and every other configuration remain prohibited.
Successful mutations include the unchanged `queryPlanProbe` and an additive
`queryPlanningProbe`. The top-level `queryPlanning` capability is version `1`,
uses method `postgresql-explain-bounded-generator-pairs`, advertises
`maxNestedLoopPairRows`, and publishes the closed
`nested_loop_pair_work` reason code. The corresponding probe contains only the
same version and method, `maxProvenGeneratedRows`, `nestedLoopCount`,
`maxEstimatedNestedLoopPairRows`, and `maxAllowedNestedLoopPairRows`.

The server derives the proven maximum from literal `generate_series` and the
existing scoped/composed H3 estimate. A `ProjectSet` applies the literal series
bound once per corrected input row; the H3 estimate is already a total scoped
pipeline bound and is never compounded across project nodes. Conservative
bounds survive filters, windows, grouped aggregates, grouping, uniqueness,
other bound-preserving nodes, and materialized CTE scans; only a proven global
aggregate or exact false one-time filter stops propagation. A nested loop is
blocked when the product of its two corrected inputs exceeds 100,000,000 rows.
This rule is query- and predicate-independent: a low-row parameterized index
inner plan can pass, while an equally risky non-spatial nested loop is rejected;
hash joins do not accrue nested-loop pair work.

Query guard failures use this stable taxonomy:

| Status and code | Category | Meaning and remediation |
| --- | --- | --- |
| HTTP `400`, `derived_layer.query_invalid` | `invalid` | The input is not exactly one parseable `SELECT` statement. Correct the syntax or statement form. |
| HTTP `422`, `derived_layer.query_not_allowed` | `policy` | The query uses a prohibited SQL construct, namespace, relation, routine, operator, cast, type, or catalog dependency. Follow the reason-specific action and use only approved, schema-qualified objects. |
| HTTP `409`, `derived_layer.query_too_expensive` | `compute` | SQL shape, generated/H3 expansion, join fan-out, recursion, or a PostgreSQL plan limit was exceeded. Reduce intermediate work or bounded expansion without changing the requested semantics. |

These responses include `error`, `userMessage`, `suggestedAction`, `code`,
`category`, `operation`, `blocked: true`, `stateUnchanged: true`, `safeState`,
`name`, `probe`, and structured `reasons` as applicable. Every reason contains
its own `code`, `message`, and `suggestedAction`; clients should present those
actions rather than substituting a generic H3 hint. None of these errors may
include `recommendedKind`: syntax, policy, and compute failures block both
ordinary and materialized views. A `nested_loop_pair_work` failure also carries
`queryPlanningProbe` beside the backward-compatible legacy `probe`; clients
should use it to guide a semantics-preserving rewrite that exposes a selective
parameterized or indexed inner input and keeps complete-input aggregates or
windows outside the row-matching path.

For materialized operations, the same plan's final row count and width plus
conservative storage overhead enforce the separately advertised 1 GiB maximum.
Successful materialized mutations also include `materializationProbe`. An
over-limit stored result returns HTTP `409`,
`derived_layer.materialization_too_large`, `blocked: true`, the probe, and
`recommendedKind: "view"`. If `probeStage` is `estimate`, no materialized DDL
or refresh starts. If it is `actual`, population and indexing occurred inside
the transaction before `pg_total_relation_size` exceeded the limit; the
probe includes `actualBytes`, the response includes `rolledBack: true`, and
PostgreSQL rolls the transaction back. That check cannot prevent transient
table, index, TOAST, or WAL growth. Materialized indexing includes the unique
feature-ID index and native, EPSG:4326, EPSG:3857, EPSG:27700, and safe
EPSG:4326 geography GiST expressions for the declared geometry. Clients should use the
operation-specific
`safeState`, prompt the user to review a create/convert-to-view fallback or
reduce the output, and never silently change kind. Only this storage error may
recommend an ordinary view, and only after the universal computation guard has
passed.

Expected guard failures from background create, replace, and refresh expose the
same envelope under `operation.error`, with the original HTTP `status` and
exception `type` added. Polling clients must surface the nested
`userMessage`, derived-layer code, `suggestedAction`, reasons, and known-state
fields rather than replacing them with a generic operation failure. An
unexpected preflight failure or failure followed by a proven rollback can use
`derived_layer.operation_failed` with authoritative unchanged-state fields.
An unexpected commit, rollback-finalization, or result-reporting failure uses
the same code but ends as `indeterminate`; clients must inspect the operation,
managed layer, and catalog before retrying. `technicalDetail`, when present on a
database error directly or under `operation.error`, is diagnostic and must not
be the primary user notification.

A proven-safe lock conflict uses HTTP or stored status `409`, code
`derived_layer.database_contention`, `category: "contention"`, and
`retryable: true`. `contentionScope` is `derived-mutation` when another
transaction owns database-wide mutation admission, or `postgresql-lock` when a
PostgreSQL lock outside that admission boundary exceeds the configured timeout.
Retry only after the blocking operation or transaction clears. Commit or
rollback uncertainty remains indeterminate, omits `retryable`, and is never
reclassified merely because its SQLSTATE is `55P03`.

`failurePhase` uses this closed vocabulary:

| Phase | Authoritative meaning |
| --- | --- |
| `preflight` | No mutation transaction or DDL began; `stateUnchanged` and `safeState` are authoritative. |
| `database-transaction` | The transaction body failed and an explicit rollback completed; `stateUnchanged`, `safeState`, and `rolledBack: true` are authoritative. |
| `transaction-rollback` | Rollback completion could not be confirmed; the outcome is `indeterminate`. |
| `transaction-commit` | Commit completion could not be confirmed; the outcome is `indeterminate`. |
| `result-reporting` | The mutation returned from its commit boundary, but its result could not be durably reported; the outcome is `indeterminate`. |
| `request-response` | The client lost the initial mutation response and cannot infer whether the server reached its commit boundary; the observed outcome is `indeterminate`. |
| `operation-polling` | The client lost or timed out while observing a durable operation; the observed outcome is `indeterminate`, and the operation ID must be reconciled. |
| `service-recovery` | The service restarted with an operation still marked running; the recovered record is `indeterminate`. |

`stateUnchanged` is present only for `preflight` or a proven
`database-transaction` rollback. `rolledBack` is present only after the server's
explicit rollback call returns successfully. Indeterminate phases omit all
three of `stateUnchanged`, `safeState`, and `rolledBack`. Safe failures direct
the caller to correct the request and retry; indeterminate failures instead
require operation, managed-layer, and catalog inspection before any retry.

Other derived-layer failures are also structured rather than overloaded as
query cost:

| Status/code | Corrective evidence |
| --- | --- |
| HTTP `422`, `derived_layer.source_mismatch` | `declaredSources`, `resolvedSources`, `missingSources`, and `extraSources` identify exactly how to make the declaration match PostgreSQL dependencies. |
| HTTP `400`, `derived_layer.source_profile_required` | Synchronize each listed source semantic profile, then inspect it before retrying. |
| HTTP `400`, `derived_layer.spatial_scope_invalid` | Select a valid workspace locale and let the server resolve its extent. |
| HTTP `400`, `derived_layer.invalid_request` | Correct the named definition/request field. |
| HTTP `404`/`409`, `derived_layer.not_found`/`derived_layer.already_exists` | List or rename/replace the layer as directed; the response states the preserved operation state. |
| HTTP `409`, `derived_layer.maintenance` or `derived_layer.in_use` | Wait for maintenance, or resolve the reported PostgreSQL/workspace dependencies. |
| HTTP `409`, `derived_layer.database_contention` | `contentionScope` distinguishes another derived mutation from a PostgreSQL lock outside mutation admission; a proven-safe result is manually retryable only after the blocker clears. |
| HTTP `422`/`500`, `derived_layer.database_error` | A preflight or proven rollback response states the preserved state and may be corrected and retried. A commit, rollback-finalization, or reporting uncertainty is explicitly indeterminate and requires reconciliation. Optional `technicalDetail` contains only bounded `sqlstate` and primary `message`, never the SQL, context, detail, or hint. |
| HTTP `500`, `derived_layer.operation_failed` | `preflight` and proven rollback failures state the preserved target; commit, rollback-finalization, and reporting failures are indeterminate and require authoritative operation/layer/catalog inspection. |

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
| `GET /api/schema` | Return the typed pinned-XYZ contract, including native template, gazetteer, dictionary, SVG, and bundled-plugin fields; `pointer` may focus the response for CLI use. Unknown contract properties are rejected with an exact path. |
| `GET /api/plugins` | Return the pinned registry and external manifest catalogue, including schemas, hashes, compatibility, usage, diagnostics, preview checks, and the catalogue fingerprint used by all `config-cli plugins` commands. |
| `POST /api/proposals/<id>/apply` | Apply a pending proposal, then wait for its fingerprint-matched reload |
| `POST /api/proposals/<id>/decline` | Record rejection and optional reason |
| `POST /api/sql/test` | Probe one calculated information expression |
| `POST /api/visual-plan` | Choose a data-aware view, with optional named `locale` and bounded `centre`/`zoom` override; a complete explicit view bypasses database auto-framing |
| `POST /api/visual-test` | Run browser validation and create report/screenshot artifacts, with the same locale and view behavior |
| `POST /api/xyz/reload` | Request a generation-based XYZ reload and wait for TCP readiness with the current workspace fingerprint |

Proposal visual requests accept `viewMode` as `focus` (the default) or
`default`. Focus mode activates the requested layer and relevant group context
through XYZ's `layers` query parameter. Default mode omits that parameter for
both original and candidate renders so initial `layer.display` changes can be
verified without the preview overriding them.

Administrator-session routes create/revoke bearer tokens and change the
administrator password. `POST /api/admin/tokens` defaults a missing `expires`
field to 30 days. A timestamp more than 30 days in the future, or an explicit
`null` for a non-expiring token, also requires
`"extendedExpiryConfirmed": true`; the server rejects ambiguous or
unconfirmed extensions.

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
to the candidate. Browser executions also bind their report and response to
the durable `operationId`. Failed operations retain any plan, diagnosis,
report, and artifact paths produced before failure; a request rejected before
browser execution returns a structured error and operation without claiming
artifacts. Only paths for files actually retained by the runner are advertised.
Visual-test and screenshot requests may set `background: true`. The server
returns `202 Accepted` with `operation` and `statusUrl`, continues browser work
independently of that HTTP connection, and atomically writes the complete
result/error envelope before the operation becomes terminal. A caller whose
local wait expires can continue polling the same operation without restarting
Chromium or losing its eventual report.
Running visual operations persist stage heartbeats in `stage` and advance
`updated`. Terminal records set `finished` to the same durable timestamp as
their final `updated`; active records are never removed by bounded history
pruning. Chromium launch, workspace load, layer registration, interactions,
screenshot capture, artifact
persistence, and the complete background operation have independent bounded
deadlines. A timeout becomes `failed` with `visual.run_timeout`,
`visual.artifact_persistence_timeout`, `visual.browser_transport_timeout`, or
the outer `visual.operation_timeout`; `failedStage`, the effective timeout,
and bounded browser console/page/request diagnostics are retained when
available. Browser crashes retain the same stage diagnostics. The runner
bounds browser shutdown before releasing its concurrency slot, so a failed run
cannot permanently reject later work. If a worker exits before its atomic
terminal write is visible, the watchdog records
`visual.result_persistence_failed`. A late worker completion cannot replace an
already terminal watchdog result.
Each browser report includes `activationDiagnostics`: configured candidate and
server-resolved locale layer keys, URL-resolved keys, configured and rendered
group membership, registered layer drawers, and separately labelled URL-hook
and actual OpenLayers visibility evidence. In focused mode every requested
foreground/background key receives a `requestedLayerVerdicts` entry covering
configuration, locale/URL resolution, group, runtime and drawer registration,
OpenLayers collection membership, visibility, and the final active-and-visible
decision. An unavailable map inspection fails closed and is reported rather
than relabelling URL/DOM state as OpenLayers state. Visible layer/group wording
is always informational because a grouped child can render while XYZ shows
only its folder label; structural registration drives the verdict.
Only a pending, integrity-valid proposal whose original
revision is still current is eligible; declined, applied, conflicted, corrupt,
or superseded proposals are rejected. The request accepts visual `layer`,
`locale`, bounded centre/zoom overrides, and viewport fields. It never accepts
an arbitrary workspace.

Automatic database-backed planning uses the layer's effective rendered
dataset. Its validated `filter.default`, `featureSet`, and `featureLookup`
restrictions are applied before feature count, extent,
representative-feature selection, and focus-bound calculation. The plan's
`effectiveDataset` records the composed locale, source relation, effective
filter/restriction descriptor, filtered count, and selected representative
feature. This descriptor is deliberately structured instead of returning
credentials or executable database text.
If no matching non-null geometry remains, planning stops before Chromium with
HTTP 422 and `code: "visual.no_matching_features"`, plus
`filteredFeatureCount: 0`, `representativeFeature: null`, a specific `reason`,
and the same `effectiveDataset` provenance. Structured fixed filters are
deeply validated; forms whose pinned XYZ behavior cannot be reproduced
deterministically are rejected before planning. Trusted predicate strings
remain subject to the existing read-only expression validation.

Proposal screenshots default to a square 1080×1080 viewport at 1× device scale
and capture only that viewport, producing an exact 1080×1080 page image.
Request overrides are bounded to a 320–2560 pixel width, 240–1440 pixel height,
and a 1×–3× device scale. The browser report's `capture` object records the
effective viewport, scale, capture mode, and actual PNG dimensions so reviewers
can verify the retained evidence resolution.

Screenshot requests may also ask the browser runner to open XYZ layer drawers:
`panel: "filtering"` for one panel, or `panels: ["filtering", "styling"]` for
multiple panels. The runner first matches the layer's exact internal
`data-id`, falling back to its visible title, then opens XYZ's stable
`filter-drawer` or `style-drawer` hook. For Filtering it also reveals the
first matching configured filter control, without selecting a filter value.
It returns cropped comparison artifacts such as
`beforeFilteringPanel`, `afterFilteringPanel`, `beforeStylingPanel`, and
`afterStylingPanel`. Optional `expectedPanelText` values are checked only
inside the opened panel, after that control is revealed; page text elsewhere
cannot satisfy them. A requested panel passes only when its exact panel was
found, opened, and captured and all expected text was present. Its report
records `found`, `attempted`, `opened`, `captured`, `expectedTextFound`,
`missingExpectedText`, and `failureReason`. Consumers must require the panel's
`passed` result and its non-null artifact path rather than treating an overall
page screenshot as panel evidence. Existing page, map, report, and
feature-information artifacts are preserved.

Active `style.hover` configuration is exercised automatically at the planned
representative feature. Visual-test and proposal preview requests may set
`hover: true` to require that evidence or `hover: false` to deliberately
suppress it. `expectedHoverText` accepts up to 20 non-empty strings and also
requires hover evidence unless hover is explicitly suppressed. The browser
report's `hover` object distinguishes `requested`, `configured`, `suppressed`,
`attempted`, and `opened`; records the point, field, observed text, captured
image dimensions, expected-text matches, and final `passed` result; and
explains a failed observation without treating generic page-text changes as
tooltip evidence. A successful capture is exposed as
`artifacts.hoverTooltip`. Proposal comparisons expose
`beforeHoverTooltip` and `afterHoverTooltip` for the corresponding original
and candidate sides.

For probeable database layers, visual planning focuses a representative
feature near the layer extent centre and records an `interaction` plan for the
browser runner. A proposal screenshot publishes the retained original and then
the candidate to the same isolated preview process and renders both at the
same view. Its `beforePage`/`beforeMap` artifacts therefore represent the
original proposal revision, while `afterPage`/`afterMap` represent the
candidate—not merely pre-click and post-click candidate states.

When both `centre` and `zoom` are supplied, the server validates them before
database planning and does not run the relation-wide feature-count, extent, or
representative-feature queries. The plan preserves the exact explicit view and
records a browser-centre interaction without claiming a preselected feature
ID. Hover and clicked-feature evidence then pass only if the browser actually
finds the expected content at that map centre. A centre-only or zoom-only
override still needs automatic framing for the missing part.

Database failures during automatic planning are read-only visual errors. A
timeout returns HTTP 422 with `code: "visual.planning_timeout"`,
`planningStage`, `queryPurpose`, and `timeoutMilliseconds`; other PostgreSQL
planning failures use `code: "visual.planning_database_error"` with the same
safe stage and purpose fields. The response does not expose SQL, relation
names, or raw driver details. These failures happen before Chromium starts, so
they do not contain a browser report or visual artifacts. Visual-test and
screenshot submissions still create and terminalize a durable operation at
the `planning` stage; metadata-only visual-plan requests do not create one.

When the focused diff changes the selected layer's `infoj` feature-information
configuration, the runner selects a representative feature, waits for XYZ's
expanded `.location-view` panel to finish loading, and uses the selected state
for that side's comparison image. An edit to an existing layer captures both
sides; an added layer captures the candidate only, and a removed layer captures
the retained original only. The response records this per-side intent and
outcome under `comparison.featureInfoEvidence` and returns cropped
`beforeInfoPanel` and/or `afterInfoPanel` artifacts for the sides that could be
captured. For other proposal changes the comparison remains unselected so a
point highlight does not hide a symbol-style change. The separate visual-test
endpoint continues to exercise the planned candidate interaction.

The evidence planner automatically expects the title or label of each changed,
visible `infoj` entry. It also extracts visible text from a deliberately narrow
constant expression such as `'<strong>Source:</strong> ONS'::text`; it never
evaluates or guesses the result of dynamic SQL. A visual-test or screenshot
request may add up to 20 bounded candidate assertions with
`expectedInfoPanelText`. The browser report's `interaction` object returns
`requested`, `attempted`, `opened`, `captured`, `failureReason`, expected-text
matches, a panel-only text sample, and the `infoPanel` artifact. Success
requires a concrete planned target, an attempted click, a newly opened stable
identity-bound panel, expected layer/feature identity, requested text, and a
retained panel capture. Missing or mismatched evidence fails at
`information-panel` instead of being reported as a generic render pass.

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

Focused previews derive `backgroundLayers` from visible `format: "tiles"`
layers in the effective locale. They never assume a particular OSM object key;
when there is no explicit focus or background layer, the runner omits the
`layers` URL parameter so XYZ can honour configured initial visibility.

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

Legacy `full` bearer tokens remain compatible. Device credentials support the
workspace scopes `inspect`, `propose`, `visual`, `apply`, `reload`, and
`derive`, plus `semantic:inspect`, `semantic:source`, `semantic:generate`,
`semantic:data`, `semantic:propose`, `semantic:apply`, and `semantic:admin`;
all are enforced
server-side. The
default agent request is
`inspect + propose + visual + semantic:inspect`. Workspace and semantic apply,
reload, derive, source synchronization, generation, data-context egress, and semantic
administration must be requested explicitly.
Direct workspace writes remain full-token/administrator-session operations.
Dashboard password changes, token issuance or revocation, device approval, and
audit routes remain administrator-session-only; a `full` bearer token does not
gain those credential-administration capabilities. Narrow scopes are not
hierarchical; a bearer token
which must inspect and retry a retained semantic-profile event needs both
`semantic:inspect` and `semantic:admin`. The retry endpoint keeps the route
name `repair`, but does not provide a way to rewrite a deterministic conflict.
The same pair is required to archive one semantic profile, archive existing
profiles selected by the configured source exclusions, or inspect an archived
asset/history by its exact retained ID.

The dashboard token form provides semantic reader, proposal author, AI
semantic author, curator, delivery operator, semantic administrator, and full
platform operator presets, plus an exact custom checklist across
workspace and semantic scopes. These are least-privilege provisioning helpers,
not additional scope semantics; the API stores and enforces the exact
submitted scopes.

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
proposal applies, explicit reloads, and background derived-layer create,
replace, or refresh work retain bounded mode-`0600` records. Reading one
requires its corresponding `visual`, `apply`, `reload`, or `derive` scope.

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
| `409` | Revision, proposal lifecycle, integrity, or active reset-maintenance conflict |
| `422` | Candidate validation or browser validation did not pass |
| `429` | Login, visual-runner, or derived-background concurrency limit reached; retry later |
| `503` | A required internal service, including semantic registration, is unavailable |
| `504` | The workspace/proposal write may already be committed, but XYZ reload confirmation did not complete |

A browser-validation `422` retains the selected plan and the browser runner's
failed report and artifact paths. Clients should surface that evidence rather
than replace it with a generic error.

A pre-browser planning `422` instead carries
`visual.planning_timeout` or `visual.planning_database_error` plus
`planningStage`, `queryPurpose`, and the failed durable operation. It has no
browser artifacts to retain.

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
