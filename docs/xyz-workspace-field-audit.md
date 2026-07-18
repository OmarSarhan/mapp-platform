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
| Layer list grouping | per-layer `group`, `groupClassList`, and `groupmeta`; XYZ creates a drawer for each shared `group` value |

## Intentional schema behavior

XYZ composes partial workspace, locale, and layer objects through templates.
The JSON Schema therefore permits partial objects and unknown plugin
properties. The dashboard's server-side validator is stricter for a concrete
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
