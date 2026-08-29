# XYZ workspace schema

The machine-readable schema is
[`config-ui/schema/workspace.schema.json`](../config-ui/schema/workspace.schema.json).
It describes the workspace surface used by this project against the pinned
GEOLYTIX XYZ v4.23.4 commit.

The contract is closed at workspace, locale, layer, gazetteer, and bundled
plugin boundaries. Unadvertised properties are rejected with their JSON path
and are never silently discarded. Maps remain open only where arbitrary names
are intrinsic to an audited feature, such as layer IDs and role names.

The top-level `locale` is XYZ's default rendered locale, including when
`locales` exists. XYZ pre-composes the default into each named locale except a
named key literally called `locale`, which resolves the top-level default
instead of becoming a distinct alternative. Its nested merge behavior is
framework-specific: objects merge by key, while arrays concatenate unless all
source items are already present, in which case the source array replaces the
target. Comma-separated locale composition uses the same framework rules.
Validators and clients must preserve those rules rather than inventing a
generic deep merge.

## Layer folders

XYZ creates layer-list folders at runtime from the optional `group` property
on each ordinary layer. Layers with the same exact, non-empty string are placed
in the same drawer:

```json
{
  "locale": {
    "layers": {
      "Bus Stops": {"group": "Transport"},
      "Rail Stations": {"group": "Transport"}
    }
  }
}
```

This is a flat layer map, not a nested folder structure. The dashboard exposes
the value as **Layer folder** and groups its own navigation the same way.
Clearing the field removes `group`. Advanced upstream properties
`groupClassList` and `groupmeta` are preserved, but remain in Advanced layer
JSON because they control CSS and trusted HTML respectively. XYZ copies
`groupClassList` from the first layer that creates a group drawer. To colour a
group, use a class already provided by the deployed map stylesheet and set the
same class list on every member so composition or ordering cannot change the
result. A hex colour is not a class, and XYZ has no native `groupColor` or
`groupColour` property.

Folder order is not map drawing order. A `group` only changes layer-list
navigation; it does not create an OpenLayers group or a shared rendering
stack. Set `zIndex` on each layer when relative rendering order matters.
Higher values render above lower values, including across different folders:

```json
{
  "locale": {
    "layers": {
      "Boundaries": {"group": "Planning", "zIndex": 10},
      "Labels": {"group": "Reference", "zIndex": 20}
    }
  }
}
```

Set `promoteDisplay: true` only when a layer should be moved above every
currently displayed layer each time it is shown. Because that behavior is
dynamic, explicit `zIndex` values are preferable for a stable, reviewable
drawing order.

## Interactive layer Styling panel

XYZ treats `layer.style` as both feature-rendering configuration and the input
to an optional interactive Styling drawer. `style.hidden: true` suppresses the
drawer only; it does not disable `default`, `highlight`, `hover`, theme, label,
or icon-scaling behavior.

`style.elements` is an ordered allow-list of panel builder keys:

```json
{
  "style": {
    "hover": {"display": true, "field": "stop_id", "title": "Feature"},
    "opacitySlider": true,
    "elements": ["hover", "opacitySlider"]
  }
}
```

The pinned framework recognizes `labels`, `label`, `hovers`, `hover`, `themes`,
`theme`, `icon_scaling`, and `opacitySlider`. Inclusion alone is insufficient:
XYZ checks that the corresponding property exists, and selector controls may
require multiple configured choices. Unknown element keys are valid extension
points and must be preserved even though the dashboard cannot preview their
plugin-provided renderers.

### Optional layer-symbol legend

XYZ renders a layer legend from `style.theme`; it does not automatically turn
`style.default` into a legend. A single-symbol layer can use an optional
`basic` theme whose style matches its default map symbol:

```json
{
  "style": {
    "default": {"strokeColor": "#ff007b", "strokeWidth": 3},
    "theme": {
      "type": "basic",
      "label": "Definitive public right of way",
      "style": {"strokeColor": "#ff007b", "strokeWidth": 3}
    },
    "elements": ["theme"]
  }
}
```

The dashboard's **Basic legend** mode creates this theme from the current
default symbol and keeps both styles synchronized when the default symbol is
edited there. Omitting `style.theme` retains the map symbol without showing a
legend.

The dashboard labels a layer as **Static symbology** when the default style,
optionally with a basic theme, supplies its one symbol. Categorized,
distributed, and graduated themes are labelled **Data-driven symbology**. For
those themes the dashboard shows the driving field(s), number of legend
classes, and previews up to eight configured category symbols. The separately
labelled default/fallback editor remains available, but is not presented as
the complete data-driven legend. Named themes are resolved from `style.themes`
for the same inspection; a missing named definition is shown as a warning.

The **Symbology mode** selector exposes the four XYZ theme implementations:

- **Basic legend** applies one theme style.
- **Data-driven categorized** matches exact values from one field, or composes
  point icons from categories attached to multiple declared fields.
- **Data-driven graduated** compares an ordered list of unique numeric breaks
  using `less_than` or `greater_than`. XYZ uses the final stored category when
  no break matches, so its label must describe the remaining range rather than
  only the final numeric break.
- **Data-driven distributed** reuses a configured style palette by stable
  feature identity and attempts not to repeat a style on intersecting
  features.

For categorized, graduated, and distributed modes:

1. Select the layer and find **Symbology mode**.
2. Choose the required data-driven mode.
3. Select the required category, numeric, or stable-identity field.
4. Use **Add legend category** for each exact value, numeric break, or palette
   entry, then provide its user-facing label.
5. Expand **Edit symbol or icon** to configure the complete point icon,
   line, or polygon style for that class.
6. Review the map/fallback preview and the richer **Feature information
   preview**, then save and reload XYZ.

For multi-field categorized point icons, enable **Compose icons from multiple
fields**, choose the contributing **Category fields**, then set each legend
category's own **Category field** and exact value. The dashboard writes
`style.theme.fields` and category-level `field` values, and deliberately hides
the single `style.theme.field` control because XYZ accepts either `field` or
`fields`, not both.

The editor writes the selected `style.theme.type` and its mode-specific fields
and categories, and ensures the theme legend is included when an explicit
`style.elements` list exists. Switching a configured theme to another mode
shows a destructive-change warning because its fields, values, labels, breaks,
and styles will be replaced. Cancelling leaves the existing theme untouched.
Named theme references are resolved from `style.themes`; selecting another
existing named theme is supported without flattening its definition.

XYZ’s OpenLayers style conversion makes symbol compatibility geometry-specific:
points require `icon`; lines use `strokeColor`, `strokeWidth`, and `lineDash`;
polygons use `fillColor` and may also use the stroke properties. Multi-field
categorized themes compose an array of icons and are therefore intended for
point layers. These rules apply equally to the default fallback, basic theme
style, and every categorized, graduated, or distributed category style.

The Feature information preview includes the selected-geometry swatch,
its synchronization status, representative attribute values, and the complete
configured categorized legend. This makes the fallback swatch and the
data-driven legend visibly distinct before a workspace change is saved.

### Optional symbol in clicked-feature information

An `infoj` entry with `type: "geometry"` uses its optional `style` both for the
selected geometry overlay and for the swatch or icon beside its checkbox:

```json
{
  "type": "geometry",
  "label": "Public right of way",
  "display": true,
  "field": "geom_3857",
  "fieldfx": "ST_AsGeoJSON(geom_3857)",
  "style": {
    "fillColor": null,
    "strokeColor": "#ff007b",
    "strokeWidth": 3
  }
}
```

Point entries may instead use the same `icon` object as a feature style. The
dashboard's **Feature-information symbol** control copies
`layer.style.default` into the geometry entry and records
`_dashboard.styleFromLayerDefault: true`. Subsequent default-symbol edits then
update only marked entries. An unmarked custom `infoj[].style` is preserved
until an operator explicitly chooses to replace it. Disabling the control
removes only a marked, dashboard-managed style.

The dashboard presents this as the explicit **Keep information symbol
synchronized** checkbox. This avoids silently overwriting custom information
styles and prevents a managed swatch drifting when the static default/fallback
symbology changes. A geometry information swatch is one static style: for a
data-driven theme it represents the fallback, while the theme legend represents
the individual data-driven classes.

## Interactive layer filters

XYZ builds a layer's Filtering drawer from its `infoj` entries. An entry can
declare `filter: true` for type inference, a built-in filter type string, or a
filter object:

The dashboard limits interactive filter choices to the entry's information
type. Text, numeric, integer, date, datetime, and boolean entries receive their
matching XYZ control and compatible exact, inclusion, exclusion, or null
choices. Types that XYZ cannot infer as filters are shown as unavailable.

```json
{
  "filter": {"viewport": true, "includeAll": false},
  "infoj": [
    {
      "title": "Town",
      "field": "town",
      "type": "text",
      "filter": {"type": "like", "leading_wildcard": true}
    },
    {
      "title": "Object ID",
      "field": "object_id",
      "type": "integer",
      "filter": true
    }
  ]
}
```

Automatic inference maps `text` to `like`, `numeric` to `numeric`, `integer`
to `integer`, `date` to `date`, `datetime` to `datetime`, and `boolean` to
`boolean`. Built-in explicit types are `like`, `match`, `numeric`, `integer`,
`in`, `ni`, `date`, `datetime`, `boolean`, and `null`.

Layer-level `filter.include`, `exclude`, and `includeAll` control which
compatible fields are offered. `filter.hidden` suppresses the drawer;
`filter.viewport` scopes generated ranges, histograms, and counts to the
current map view. `filter.default` is a fixed server-side restriction composed
with interactive filters. The backend deeply validates the deterministic
object and top-level OR-array forms, including operation operands. It rejects
field-level OR arrays and dynamic request-user operands whose pinned behavior
cannot be reproduced reliably during planning. Upstream also accepts trusted
template SQL strings there, so changes require explicit query and data-access
review.

Use `config-cli layers statistics LAYER FIELD` for a bounded distribution of a
stored numeric field after the layer's fixed filter and identifier restrictions
are applied. The response contains no rows: it reports null/finite counts,
min/max, fixed quantiles, histogram bins, requested threshold counts, and
candidate class counts with exclusive upper bounds. This is the appropriate
evidence for deciding whether equal-width classes are useful and whether a
highest-value boundary needs one increment of headroom. Style and filter on
the raw numeric field; reserve formatted text for hover and clicked-feature
information.

XYZ only creates the Filtering drawer, including its count, when at least one
compatible `infoj` entry is offered by `filter.includeAll`, `filter.include`,
or the entry's own `filter` property. `filter.count_meta` optionally replaces
the text after the number. Omitting `filter.viewport` preserves the ordinary
non-viewport count.

Interactive filters must target real output columns from the layer relation.
Calculated `infoj[].fieldfx` entries are safe for clicked feature information,
but XYZ v4.23.4's Filtering drawer builds SQL and numeric min/max statistics
from the literal `field` name and does not expand `fieldfx`. To filter a
calculated value, expose it as an actual source or derived-layer column first;
do not rely on an `infoj` alias such as `resurface_cost_rounded`.

Pinned XYZ v4.23.4 accepts `filter.viewport_description` and constructs an
element for it, but leaves that element hidden. The dashboard therefore
preserves an existing value in Advanced layer JSON but does not advertise an
editable control that has no visible framework effect.

### Optional count beside the layer name

A queryable database layer can show its current viewport count in brackets
directly beside its name in the layer list:

```json
{
  "plugins": ["/instance/plugins/viewport-layer-count.mjs"],
  "viewport_layer_count": {},
  "filter": {"viewport": true}
}
```

The dashboard's **Show viewport count beside layer name** switch manages these
properties together. The badge uses XYZ's `location_count` query, refreshes
after map movement, respects the layer's active filters, and queries only while
the layer is visible and has a table at the current zoom. It shows an ellipsis
while loading and an en dash if the count is unavailable. A custom
`viewport_layer_count.debounce` from 0 to 5000 milliseconds may be supplied in
Advanced layer JSON; the default is 250 milliseconds in addition to XYZ's
map-change debounce.

Omitting `viewport_layer_count` leaves the layer heading unchanged. Tile and
other non-queryable layers do not support this badge.

The dashboard exposes the safe common controls and preserves fixed ranges,
value lists, dropdown/search presentation, histograms, drawer/dialog options,
and custom extensions in Advanced layer JSON.

When raw `workspace.locale` is absent, XYZ synthesizes
`{"layers": {}}` as the default. No locale selection and the explicit name
`locale` both resolve that synthetic default, even if exactly one named
alternative exists. Named non-`locale` alternatives compose over the
synthetic base.

The dashboard renders effective named locales for inspection but makes their
workspace, add/remove, ordinary layer, and Advanced JSON controls read-only.
Effective validation and visual/SQL testing remain available through the
server API and CLI. Editing an effective object could otherwise copy inherited
content into the raw override, and deleting an inherited layer would not
remove it from the default. Use focused API/CLI JSON Pointer operations against
the raw `locales.<name>` override instead.

## Dashboard layer editor structure

The editable default-locale layer page is divided into task-based collapsible
sections: **Identity and display**, **Data source**, **Appearance and legend**,
**Interaction**, **Feature information**, and **Advanced layer JSON**.
Database controls appear only for supported single-relation database layers;
tile layers show their URI instead; open-ended external/template sources
retain their complete JSON without presenting inapplicable database fields.
The server catalog omits tables in PostgreSQL's `public` schema when offering
new layers. Existing workspace layers that explicitly reference `public.*`
remain supported and are still checked by validation.
Styling controls are disabled until the Styling panel is enabled, and
Filtering-panel options are disabled or hidden until that panel is enabled.
Advanced JSON remains available so this organization never discards unknown
XYZ extension properties.

## Hierarchy

```text
workspace
├── key
├── dbs
├── locale                         default rendered locale
│   ├── name
│   ├── extent
│   │   ├── north / east / south / west
│   │   └── mask
│   ├── view
│   │   └── lat / lng / z / minZoom / maxZoom
│   ├── mapviewControls[]
│   ├── ScaleLine
│   ├── template / templates[]
│   ├── plugins[] / syncPlugins[]
│   ├── keyvalue_dictionary
│   ├── svgTemplates / svg_templates
│   ├── built-in plugin configuration (zoomBtn, locator, login, test, zoomToArea)
│   ├── plugin-owned extension objects
│   └── layers
│       └── <layer key>
│           ├── name / display / format
│           ├── attribution
│           ├── tiles: URI / source / projection / params
│           ├── data: dbs / table / geom / srid / qID
│           ├── gazetteer             optional native layer search panel
│           ├── infoj[]
│           │   └── type / title / label / field / fieldfx /
│           │       display / inline
│           └── style
│               ├── default
│               ├── highlight
│               └── opacity
├── locales                        optional named composed locales
│   └── <locale key>               override composed with default by XYZ rules
└── templates                      query, locale, layer and module templates
```

## Keys and options

| Path | Type/options | Meaning |
| --- | --- | --- |
| `key` | non-empty string | Stable workspace identifier. |
| `dbs` | letters, numbers, underscores (56 char max) | Default connection; `MAPP` resolves from `DBS_MAPP`. |
| `locale.name` | string | Human-readable locale title. |
| `locale.extent.north/south` | number, `-90..90` | Latitude bounds. North must be at least south. |
| `locale.extent.east/west` | number, `-180..180` | Longitude bounds. XYZ v4.23.4 expects east to be at least west. |
| `locale.extent.mask` | boolean | Shades the map outside the configured extent. |
| `locale.view.lat/lng` | latitude/longitude | Initial map centre. |
| `locale.view.z` | number, `0..30` | Initial zoom. |
| `locale.mapviewControls[]` | OpenLayers control names | This file uses `Zoom`; unknown names are ignored by XYZ. |
| `locale.ScaleLine` | `metric`, `imperial` | Scale-line units in XYZ v4.23.4. |
| `templates.<key>` | object | Native XYZ template. Supported metadata includes `src`, inline `template`, `dbs`, `module`, `nonblocking`, `statement_timeout`, `value_only`, `reduce`, roles, and query access flags. |
| `locale/layer.template` | key or object | One template composed into the object. |
| `locale/layer.templates[]` | keys or objects | Templates composed in order. These are composition references, not a generic array of `{src, dbs}` includes. |
| `layer.gazetteer` | object | Native layer-panel location search. A direct `qterm` searches the owning layer; `datasets[]` add searches and require `qterm`, with optional layer/table/query/limit/label/no-result overrides. Latitude/longitude input works without a dataset. A named `provider` requires separately loaded code because the pinned utility registry supplies no external provider. |
| `locale.keyvalue_dictionary`, `layer.keyvalue_dictionary` | array | Native recursive value substitutions keyed by property name and current value, with `default` and language keys. |
| `locale.svgTemplates` / `svg_templates` | object | SVG source map; the underscored spelling is the supported legacy alias. |
| `locale.plugins[]`, `syncPlugins[]` | string arrays | Plugin modules to load, and plugin keys that must execute sequentially. |
| `locale.layers.<key>` | object | Machine key used in URLs, hooks, and queries. The pinned XYZ route accepts only ASCII letters, numbers, spaces, colons, underscores, and hyphens, but new proposal keys outside `[A-Za-z0-9_]+` receive a strong stability warning. Prefer letters, numbers, and underscores; put spaces, punctuation, and translated wording in `layer.name`. An unsupported key is omitted before browser registration and cannot be activated by `layers=`. |
| `layer.name` | string | Display label; defaults to the layer key. |
| `layer.filter.default` | predicate string, filter object, or OR-array | Fixed server-side filter applied by XYZ and by visual planning. Predicate strings are validated as one read-only expression; planning counts, frames, and selects features only from matching rows. |

### Pinned plugin capabilities

Pinned XYZ v4.23.4 dispatches a locale property to a same-named plugin. The
schema advertises only the plugins present in this commit's bundled registry:

| Locale property | Pinned behavior |
| --- | --- |
| `admin` | Adds the XYZ administrator link, but only for an authenticated administrator and a standard map button column. Its configuration object is otherwise unused. |
| `consent` | Shows `text` in a confirmation dialog for a logged-in user and persists an affirmative response in user IndexedDB; optional `title` overrides the dialog heading. |
| `custom_theme` | Passes the configured CSS colour-variable map to XYZ's colour-theme utility. |
| `dark_mode` | Adds a light/dark toggle. The chosen state comes from and is persisted to the authenticated user record; the configuration object is unused. |
| `feature_info` | Adds a click-identification mode and popup of raw feature properties. `features` expands cluster members and `css` styles the popup. `true` is also accepted upstream. |
| `fullscreen` | Toggles XYZ's fullscreen body class and resizes OpenLayers and Mapbox-backed layers. |
| `layer_order` | Sorts the decorated locale layer array using the listed layer keys. Unlisted keys sort above listed entries under the pinned comparator. This is runtime ordering, separate from `zIndex`. |
| `link_button` | Adds one or more links. Each requires `href` and `icon_name`; optional fields include `title`, `target`, CSS class/style, and `locale` query-string propagation. |
| `locator` | Adds browser geolocation and uses `locale.maxZoom` after a position is found. |
| `login` | Adds login/logout navigation only when the rendered page advertises login support. |
| `svg_templates` | Legacy plugin spelling for loading named SVG source URLs. `svgTemplates` is the preferred native property and is loaded before synchronous rendering. |
| `test` | Runs the requested `core` or `integrity` browser suite from the URL test hook; supports `quiet` and `showSummary`. |
| `userIDB` | Developer-facing JSON editor for the authenticated or anonymous user IndexedDB record. |
| `userLayer` | Developer-facing JSON editor that decorates an unsaved client-side layer; it requires XYZ's layer JSON editor UI. |
| `userLocale` | Allows an authenticated user to save and remove composed personal locales. |
| `zoomBtn` | Adds zoom buttons bounded by the effective view's minimum and maximum zoom. |
| `zoomToArea` | Adds a drag-box zoom interaction and restores pointer highlighting when finished. |

`measure_distance`, `query_features`, `posthog`, `googleMaps`, `userSettings`,
`info_panel`, `screenshot`, `coordinates`, `streetview`, and similarly named
objects are not in the pinned registry or otherwise read by this commit. They
are therefore not advertised by the schema. Unknown properties remain
round-trip-preserved so an inspection or unrelated edit does not destroy
custom data, but preservation must never be presented as feature support.

### Plugin loading and dispatch

`locale.plugins` supplies dynamic module URLs. XYZ prepends every
`layer.plugins` array, removes duplicate source strings, and considers only
values ending exactly in `.js` or `.mjs`. Relative values resolve against the
page origin; values beginning with `http` pass directly to dynamic `import()`.
Import failures are logged, but `Promise.allSettled` allows map creation to
continue.

The loader ignores module exports. A dynamic module must register its callable
as a side effect on global `mapp.plugins` under its configuration key. It runs
arbitrary browser JavaScript in the XYZ origin.

After loading, `locale.syncPlugins` keys execute sequentially as
`mapp.plugins[key](locale[key], mapview)` and each result is awaited. Other
locale keys matching registered functions execute together and are awaited
with `Promise.all`. Missing keys are silently skipped. During layer decoration,
every layer property whose name matches a registered plugin invokes that
function with the complete layer object; this layer hook is not awaited.

Use `/api/plugins` or `config-cli plugins list/show/validate/usage` for the
connected server's exact built-in and external registry. External manifest
schemas are composed into `/api/schema`; proposals and preview evidence bind
to the catalogue fingerprint. See [External XYZ plugins](external-plugins.md).

The dashboard exposes top-level templates and advanced locale/layer values as JSON object
editors. The configuration API and standalone CLI expose the same values
through the workspace document, JSON Pointer mutations, and `/api/schema`.
Properties outside the connected closed contract are rejected.

`src` supports `file:`, `cloudfront:`, HTTPS, loopback-only HTTP, and providers
created by configured `SIGN_*` environment entries. HTTPS responses ending in
`.json` are parsed as JSON; other HTTP responses are text. Templates are
fetched by XYZ when first resolved and cached for that XYZ workspace
generation (module templates are the exception). A saved workspace
reload therefore starts a fresh generation and causes live sources to be read
again on first use. Validation checks the descriptor, but deliberately does not
execute remote template code or SQL; post-reload and visual tests remain
necessary evidence for live-template changes.

When the dashboard creates a layer or a display-name edit loses focus, it
derives the internal layer key from `layer.name`: spaces become underscores,
special characters are removed, and collisions receive `_1`, `_2`, and later
suffixes. The display label itself retains its spaces and punctuation. Matching
named-locale overrides are moved with a renamed default-locale layer.
| `layer.display` | boolean | Initial visibility. |
| `layer.format` | XYZ format key | `cluster`, `geojson`, `googleMapTiles`, `mapboxStyle`, `maplibre`, `mvt`, `tiles`, `vector`, or `wkt`. The dashboard currently creates the database and tile formats used by this deployment. |
| `layer.template` | string | Workspace template merged into the layer; may supply the source properties otherwise required by a concrete layer. |
| `layer.attribution` | `{label: URI}` | Credits shown for the layer. |
| `layer.URI` | string | Required for `tiles`; supports tile placeholders such as `{z}/{x}/{y}`. |
| `layer.source` | OpenLayers source name | Tile source; defaults to `OSM`. Use `XYZ` for ordinary URL-template tiles. |
| `layer.projection` | projection string | Tile projection; defaults to `EPSG:3857`. |
| `layer.dbs` | database key | Optional connection override; inherits workspace `dbs`. |
| `layer.table` | `table` or `schema.table` | PostgreSQL relation used for feature queries; it may be a table, view, or materialized view. Managed multi-source results use `derived_layers.<name>`. |
| `layer.geom` | SQL identifier | Geometry column. |
| `layer.srid` | positive SRID | Must be `3857` for `mvt` in XYZ v4.23.4. |
| `layer.qID` | SQL identifier | Unique feature ID column. |
| `layer.tables`, `geoms` | zoom-keyed objects | Select a relation or geometry column according to map zoom. |
| `layer.params` | object | Query fields, viewport/zoom flags, and query-template overrides. |
| `layer.cluster` | object | Exactly one of client `distance` or database `resolution`, with optional `hexgrid` and `label`. |
| `featureFormat` | parser key | Response parser used by vector formats, including `geojson`, `wkt`, `cluster`, and `wkth3`. |
| `featureSet`, `featureLookup` | arrays | Restrict features and/or provide client-side feature properties. |
| `wkt_properties` | boolean | Load MVT feature properties separately through the WKT query. |
| `layer.infoj[]` | array | Ordered feature-information definitions. |
| `infoj[].type` | XYZ entry type | Defaults to `text`; common values here are `geometry` and `pin`. The schema lists all non-deprecated v4.23.4 entry handlers. |
| `infoj[].field` | SQL/result identifier | A database column when `fieldfx` is absent; otherwise the unique result alias for the calculated value. |
| `infoj[].fieldfx` | PostgreSQL expression | Select expression returned under the `field` alias. The dashboard treats column and expression sources as mutually exclusive choices. |
| `infoj[].type` | XYZ entry renderer | The dashboard probes the selected column/expression and blocks saves when its PostgreSQL result is incompatible with the renderer, such as scalar JSON for `pills`, text for `boolean`, or malformed coordinates for `pin`. |
| `infoj[].title`, `label` | string | User-facing text. |
| `infoj[].display` | boolean | Whether the entry is displayed; defaults to true. |
| `infoj[].inline` | boolean | Places title and value on one line. |
| `infoj[].style` | feature style or null | For a geometry entry, styles the selected geometry and renders the matching swatch/icon beside its information-panel checkbox. |
| `infoj[]._dashboard.styleFromLayerDefault` | boolean | Dashboard ownership marker enabling synchronization from `layer.style.default`; omit for custom entry styles. |
| `style.default` | feature style | Normal feature symbol. |
| `style.highlight` | feature style | Selected/hovered feature override. |
| `style.selected`, `style.cluster` | feature styles | Overrides for selected locations and clustered points. |
| `style.theme`, `themes` | theme object(s) | Basic, categorized, graduated, or distributed feature styling. |
| `style.hover`, `hovers` | hover object(s) | Feature hover tooltip configuration. The dashboard exposes display, field, title, and dynamic-query controls; XYZ also supports named `hovers` through advanced JSON. |
| `style.label`, `labels` | label object(s) | Feature labels, font, colours, offsets, and zoom bounds. |
| `style.icon_scaling` | object | Scales point icons using a feature field. |
| `fillColor`, `strokeColor` | CSS hex colour | Fill and outline colours. Alpha hex is supported. |
| `fillOpacity`, `strokeOpacity` | number, `0..1` | Fill/outline transparency. |
| `strokeWidth` | number, `>= 0` | Outline width in pixels. |
| `lineDash` | non-negative number array | OpenLayers stroke dash/gap sequence, such as `[5, 4]`. |
| `scale` | number, `0.1..10` | Normal icon scale multiplier. |
| `highlightScale` | number, `0.1..10` | Icon scale multiplier used by `style.highlight`. |
| `icon.type` | XYZ symbol name | `dot`, `target`, `triangle`, `square`, `diamond`, `semiCircle`, `circle`, `markerLetter`, `markerColor`, or `template`. |
| `icon.fillColor` | hex colour | Effective for the filled built-in symbols; `dot` does not use `strokeColor`. |
| `icon.strokeColor`, `strokeWidth` | colour and number | Effective for the built-in `circle` symbol. |
| `icon.color`, `letter` | colour and one character | Outer pin colour and centre text used by a layer `markerLetter`. These values must be inside the icon object. |
| `icon.colorMarker`, `colorDot` | colours | Required by `markerColor`. |
| `icon.url`, `svg` | non-empty string | Custom icon source supported by XYZ. `svg` is a legacy alias normalized to `url` at load time. Platform-managed files live in `instance/public/svg` and use `/instance/svg/<filename>.svg`. |
| `icon` | object or object array | XYZ may render one icon or multiple icons. The dashboard preserves icon arrays but exposes them as read-only outside Advanced layer JSON. |

XYZ also uses `markerLetter` internally for selected-location UI pins. That is
not the same mapping as a layer's `style.<state>.icon`. XYZ clones
`locale.locations.pinStyle`, then overwrites its `color` with the selected
location style's `strokeColor` and its `letter` with the location record's
`symbol`. A record-level `colour` first replaces the location style's stroke
and fill colours. As a result, `locale.locations.pinStyle.color` and
`locale.locations.pinStyle.letter` are not effective per-location controls,
and changing only `locale.locations.style.fillColor` does not recolour the UI
pin.

The `locations` configuration and its records are open-ended XYZ UI extension
state rather than the dashboard's validated layer-style surface. They are
preserved through `additionalProperties`; their runtime-generated `colour` and
`symbol` values should not be mistaken for required workspace-schema fields.
This behavior is verified against XYZ v4.23.4's
[`markerLetter` symbol](https://github.com/GEOLYTIX/xyz/blob/a6f03c07dd7aaae2e9ab04087143ee0400e15cb9/lib/utils/svgSymbols.mjs#L157-L167)
and
[`listview` pin construction](https://github.com/GEOLYTIX/xyz/blob/a6f03c07dd7aaae2e9ab04087143ee0400e15cb9/lib/ui/locations/listview.mjs#L267-L279).

Extent and view objects may be absent or partial where XYZ composition supplies
the remaining values. Validation checks only the numeric members that are
present and checks cross-field ordering when both sides of a bound exist.

The dashboard's ordinary catalog controls are intended for a concrete
database-backed layer with one relation reference in `table`, plus `geom` and
`qID`. The relation may be a managed view in `derived_layers`, so the workspace
layer remains conventional even when its result derives from multiple source
tables. External map styles,
Google/tile sources, template-driven layers, inline `features`, zoom-keyed
`tables`/`geoms`, icon arrays, and named style references are preserved but
remain read-only in controls that would otherwise flatten or overwrite them.
Their complete definition remains available in Advanced layer JSON and is
still subject to known schema and safety checks on save.

Advanced JSON remains editable for the top-level default locale. In a composed
named locale it is display-only, as are all other dashboard mutation controls.

The dashboard's calculated-value editor can test one `fieldfx` independently.
It returns a live non-null sample and PostgreSQL type from a read-only,
time-limited query before the complete workspace is validated or saved.

The dashboard only offers SVG files that pass its bounded safety checks and
revalidates every selected `/instance/svg/` URL when saving. XYZ serves the
same directory from its existing public static route and passes `icon.url`
directly to the OpenLayers icon renderer.

Point highlights must override effective point properties. For example, a
highlighted dot uses `style.highlight.icon.fillColor` and
`style.highlight.highlightScale`; a top-level `strokeColor` does not recolour
a dot.

## Format-dependent minimums

```text
tiles
└── format + (URI or template)

mvt
└── format + (template or relation/geometry mapping + srid=3857 + qID)

cluster / geojson / vector / wkt
└── format + (template, inline features, or relation/geometry mapping)
   + srid + qID

mapboxStyle / maplibre
└── format + style

googleMapTiles
└── format + apiKey
```

Concrete database-backed layers inherit the root `dbs` value when their own
`dbs` is omitted. The project validator additionally checks configured
database names, cross-field extent/view relationships, non-null/unique `qID`
values, unique `infoj` fields, and SQL safety. Advanced sources that cannot be
represented by one concrete relation are preserved without a misleading
catalog probe. These checks cannot all be expressed portably in JSON Schema.

The optional `layer._dashboard.generated` object records which values were
inferred by the configuration dashboard. XYZ ignores this provenance object.

---

## Configuring the workspace

The dashboard edits the live workspace through server-side validation. It
discovers PostGIS relations visible to the read-only XYZ role, validates
geometry and feature identifiers, checks calculated information expressions,
and runs a bounded render probe before saving. Every successful dashboard save
atomically replaces the live workspace, requests an XYZ restart, and waits for
the XYZ supervisor to report TCP readiness with the exact saved workspace
fingerprint. The dashboard shows the restart in progress and then confirms
that connection readiness; operators do not need to issue a second reload.

The top-level `locale` remains XYZ's default rendered locale even when
`locales` is present. XYZ composes that default into each named locale except a
named key literally called `locale`, because that name resolves the top-level
default rather than a distinct alternative. XYZ's rules include conditional
array concatenation/replacement and are not equivalent to a generic deep
merge. The dashboard, API, and CLI select the top-level default when no name is
requested and resolve named alternatives with the same composition semantics.
If raw `workspace.locale` is absent, XYZ synthesizes an empty
`{"layers": {}}` default; neither an omitted locale nor the name `locale`
auto-selects a sole named alternative.
Because a composed value may be inherited from several raw properties, named
effective locales are inspectable in the dashboard and testable through the
server API/CLI, but read-only in dashboard controls. Use focused
`config-cli`/API proposal operations against the raw named override to edit one
without flattening inherited content.

XYZ also supports external renderers, templates, inline features, zoom-keyed
tables/geometries, icon arrays, and named style references. The platform
preserves those advanced forms. The dashboard keeps their ordinary
database-specific controls read-only and exposes their complete JSON for
expert editing, because they cannot be represented safely as one catalog
relation. When such a layer is viewed through a composed named locale, its
entire dashboard editor remains read-only under the named-locale rule above.

Use the dashboard for interactive administration. Use the separately installed
`config-cli` for remote, JSON-first automation:

1. Inspect the server identity, contract, current revision, layer, schema,
   rules, and catalog.
2. Create the smallest revision-bound proposal.
3. Present the explanation, focused diff, warnings, and visual evidence.
   Top-level visual commands inspect the current live workspace; when the
   server advertises proposal preview commands, use them to render the stored
   pending candidate in the isolated preview process before approval.
4. Apply only after explicit approval. A successful apply automatically
   requests and waits for the same fingerprint-matched XYZ reload.
5. Check the returned XYZ reload status and run a post-apply visual test.

Do not directly edit a remote `workspace.json`. The platform API is the remote
write boundary, records proposal and audit state, and is what triggers the
managed reload. Direct filesystem edits are intentionally not watched. Prefer
scoped, expiring device credentials for agents; legacy full tokens remain
available for operators and migration as documented in
[Security](security.md).

The dashboard's **Semantic catalog** exposes generated and curated profiles,
orphaned annotations, immutable per-asset history, and the reviewed semantic
proposal workflow. **Access and audit** offers named least-privilege semantic
token presets or exact custom scope selection. Gemini drafting is metadata-only
by default, with separate `semantic:data` opt-ins for bounded samples or
statistics. Source exclusions are deployment configuration; administrators can
archive matching existing profiles or one selected profile without changing
the database, while retained exact-ID history remains auditable.

The public custom SVG catalog is versioned under
[`instance/public/svg`](../instance/public/svg). SVGs are exposed as
`/instance/svg/<filename>.svg` after bounded safety checks.

The machine-readable workspace schema is
[`config-ui/schema/workspace.schema.json`](../config-ui/schema/workspace.schema.json).
See [Workspace schema](workspace-schema.md) and the
[XYZ field audit](xyz-workspace-field-audit.md).
