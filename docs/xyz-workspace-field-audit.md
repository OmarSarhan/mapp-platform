# XYZ v4.23.4 workspace field audit

This audit records the configuration surface checked against the pinned XYZ
commit `a6f03c07dd7aaae2e9ab04087143ee0400e15cb9`. The machine-readable result is
[`config-ui/schema/workspace.schema.json`](../config-ui/schema/workspace.schema.json).

## Sources inspected

- `mod/workspace/*` for workspace, locale, template, and role composition.
- `lib/mapview/_mapview.mjs` for locale extent, view, controls, scale line,
  plugins, and SVG templates.
- `lib/layer/format/*` for every exported layer format.
- `lib/layer/decorate.mjs`, `featureFields.mjs`, and `featureFormats.mjs` for
  shared layer, query, feature, and zoom-dependent settings.
- `lib/layer/styleParser.mjs`, `featureStyle.mjs`, `themes/*`,
  `utils/olStyle.mjs`, and `utils/svgSymbols.mjs` for styling.
- `lib/ui/locations/entries/*` and `infoj.mjs` for feature-information entries.
- Upstream `tests/assets` workspaces and layer fixtures for composition cases.

## Audited configuration groups

| Group | Fields and variants covered |
| --- | --- |
| Workspace | `key`, `dbs`, `locale`, `locales`, `templates` |
| Locale | `name`, `role`, `roles`, `srid`, `extent`, `view`, zoom limits, map controls, `ScaleLine`, plugins, sequential plugins, SVG templates, query parameters, nested locales and layers |
| Layer composition | `template`, `templates`, `src`, included/excluded properties, roles |
| Formats | `cluster`, `geojson`, `googleMapTiles`, `mapboxStyle`, `maplibre`, `mvt`, `tiles`, `vector`, `wkt` |
| Database layers | `dbs`, `table`/`tables`, `geom`/`geoms`, `srid`, `qID`, `z_field`, query parameters |
| Tile/vendor layers | `URI`, `source`, `projection`, Google `apiKey`, Mapbox `accessToken`, style URL/object, drawing-buffer option |
| Feature loading | inline `features`, `featureFormat`, `featureSet`, `featureLookup`, lookup ID, separate WKT properties, transition, cache size, vector-image mode |
| Clustering | mutually exclusive `distance`/`resolution`, `hexgrid`, label field |
| Layer behavior | display, opacity, z-index, fade, promote-on-display, zoom display, filters, attribution, info ordering/skipping |
| Feature style | fill/stroke colours and opacity, width, dash arrays, z-index, scale variants, icons and labels |
| Layer style | default, highlight, selected, cluster, theme(s), hover(s), label(s), icon scaling, cache, layer opacity, tile context filter |
| Themes | basic, categorized, graduated, distributed; fields, categories, category styles/icons, breaks and distributions |
| Icons | every built-in v4.23.4 SVG symbol, custom URL/SVG/template sources, symbol-specific colours, letter, scale and anchor |
| Feature information | the complete active and legacy type registry plus field/query/key entries, display/edit flags, groups, JSON extraction, fallback/skip behavior, formatting, dependencies, tooltips, tabs and links |
| Layer list grouping | per-layer `group`, `groupClassList`, and `groupmeta`; XYZ creates a drawer for each shared `group` value and copies CSS classes from the first member that creates it |

## Template and plugin-extension follow-up

The template loader at this commit accepts provider-qualified `src` values,
inline `template` text, `module`, and query controls including `dbs`,
`nonblocking`, `statement_timeout`, `value_only`, and `reduce`. Locale and layer
`template`/`templates` values are ordered composition references and may be
keys or inline descriptor objects.

Layer-panel gazetteer datasets, recursive `keyvalue_dictionary` replacement,
and both `svgTemplates` and legacy `svg_templates` are native. Locale keys are also a
plugin dispatch surface. The machine schema enumerates the bundled registry:
`admin`, `consent`, `custom_theme`, `dark_mode`, `feature_info`, `fullscreen`,
`layer_order`, `link_button`, `locator`, `login`, `test`, `userIDB`,
`userLayer`, `userLocale`, `zoomBtn`, and `zoomToArea` (plus the legacy
`svg_templates` dispatch). It does not advertise configuration families absent
from this registry. Unknown keys remain permitted solely for lossless
round-tripping, not as a support claim.

The supplied `googleMaps`, `measure_distance`, `query_features`, `posthog`,
`userSettings`, `info_panel`, `screenshot`, `coordinates`, and `streetview`
names had no reader in the pinned `lib` or `mod` trees. `query_features` toolbar
and table shapes and `measure_distance` route shapes likewise had no match.
They may belong to external or older application plugins but are not pinned
framework capabilities.

The proposed locale-level `gazetteer` placement also has no consumer in this
commit. The native panel is created from `layer.gazetteer`; it injects the
owning layer and mapview before invoking the shared Gazetteer UI. External
provider names are checked dynamically, but pinned core only exports database
dataset search and coordinate-result handling.

## Supplied workspace-tree verdict

| Supplied area | Verdict at v4.23.4 | Effective behavior |
| --- | --- | --- |
| Root `dbs` | Native | Default `DBS_<key>` connection inherited by templates/locales/layers when no nearer override exists. |
| Root `templates` | Native | Lookup registry for queries and object composition. `src` is lazy-loaded; inline `template` is content; query flags control connection, response shape, timeout, and nonblocking execution. |
| Locale `name`, `roles` | Native | Name labels and composes nested locales. Roles gate access; `*` is unrestricted, `!role` is negated, and matching object values merge role-specific overrides. |
| Locale `extent`, `view`, `minZoom`, `maxZoom`, `ScaleLine` | Native | Builds the OpenLayers projection/centre/zoom/extent; optional extent mask adds a world-minus-extent overlay; scale units normalize to metric unless exactly imperial. |
| Locale `queryparams` | Native | Shallow-merged into each resolved layer's own query parameters. |
| Locale `plugins`, `syncPlugins` | Native loader | Module references load first; named synchronous plugins execute in order; remaining locale keys matching registered plugin functions execute concurrently. |
| Locale `svg_templates` | Native legacy alias | Copied to preferred `svgTemplates`; source URLs are fetched before synchronous feature-style use. |
| Locale `template`, `templates[]` | Native composition | A key or descriptor is resolved and merged. Multiple entries apply in order with XYZ's merge and role rules; they are not generic runtime query includes. |
| Locale `test`, `zoomBtn`, `login`, `locator`, `zoomToArea` | Bundled plugins | Implement browser tests, zoom controls, login navigation, browser location, and drag-box zoom respectively, subject to their runtime prerequisites. |
| Locale `googleMaps` | Not found | No locale reader or bundled plugin. `googleMapTiles` exists separately as a layer format. |
| Locale `gazetteer` | Wrong placement | Rejected. Use `layer.gazetteer` for the pinned layer drawer search panel. |
| Locale `measure_distance`, `query_features`, `posthog` | Not found | No module, registry entry, or matching test/config consumer in the pinned trees. |
| Locale `userSettings`, `info_panel`, `screenshot`, `coordinates`, `streetview` | Not found as locale capabilities | Some words occur in unrelated APIs, dictionary text, geometry, or comments, but no same-named locale reader/plugin implements these objects. |
| Locale `keyvalue_dictionary` | Native | Recursively replaces matching string property/value pairs before mapview decoration, choosing the active language value, then `default`, then the original. |
| Layer `keyvalue_dictionary` | Native | The same recursive replacement runs before layer decoration. |
| Layer `template`, `templates`, `src`, roles, filter, style, draw and documented data/query settings | Native composition | Resolved by layer composition/decorators and the format/panel modules. The schema accepts audited layer fields and rejects unknown ones. |
| Layer `gazetteer` | Native | Creates the Gazetteer panel; supports coordinate input and database searches using the owning layer or dataset overrides. |
| Named layer options such as `draft_trade_zones_live`, `theme_picker`, `isurf` | Not established by their names | Only supported if an audited core reader or loaded plugin consumes them. They are not advertised merely because an old workspace contains them. |

## Intentional schema behavior

XYZ composes partial workspace, locale, and layer objects through templates.
The JSON Schema permits partial objects but rejects unadvertised properties at
contract boundaries. Named maps remain open only where arbitrary keys are part
of the audited behavior. The dashboard's server-side validator is stricter for a concrete
saved database layer: it checks configured `DBS_*` connections, live catalog
columns, SRIDs, SQL expressions, and an XYZ-equivalent render probe.

The pinned workspace/cache path keeps top-level `workspace.locale` as the
default and composes it into named `workspace.locales` entries except a key
literally called `locale`. This uses XYZ's merge helper, not a generic deep
merge: nested objects merge, while arrays concatenate unless all source items
are already present, in which case the source replaces the target.
If `workspace.locale` is absent, the cache path synthesizes
`{layers: {}}`; default selection still resolves that empty locale rather than
auto-selecting a sole named entry.

The upstream fixture workspaces under `tests/assets` were checked against the
schema after this audit. All complete workspace fixtures validate, as does this
project's current workspace.
