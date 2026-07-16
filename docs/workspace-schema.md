# XYZ workspace schema

The machine-readable schema is
[`config-ui/schema/workspace.schema.json`](../config-ui/schema/workspace.schema.json).
It describes the workspace surface used by this project against the pinned
GEOLYTIX XYZ v4.23.4 commit.

XYZ workspace objects are extension points for templates, plugins, roles,
filters, themes, and custom UI. The schema therefore validates known values but
keeps `additionalProperties: true` at extensible object levels.

The top-level `locale` is XYZ's default rendered locale, including when
`locales` exists. XYZ pre-composes the default into each named locale except a
named key literally called `locale`, which resolves the top-level default
instead of becoming a distinct alternative. Its nested merge behavior is
framework-specific: objects merge by key, while arrays concatenate unless all
source items are already present, in which case the source array replaces the
target. Comma-separated locale composition uses the same framework rules.
Validators and clients must preserve those rules rather than inventing a
generic deep merge.

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
│   └── layers
│       └── <layer key>
│           ├── name / display / format
│           ├── attribution
│           ├── tiles: URI / source / projection / params
│           ├── data: dbs / table / geom / srid / qID
│           ├── infoj[]
│           │   └── type / title / label / field / fieldfx /
│           │       display / inline
│           └── style
│               ├── default
│               ├── highlight
│               └── opacity
├── locales                        optional named composed locales
│   └── <locale key>               override composed with default by XYZ rules
└── templates                      optional open-ended XYZ templates
```

## Keys and options

| Path | Type/options | Meaning |
| --- | --- | --- |
| `key` | non-empty string | Stable workspace identifier. |
| `dbs` | letters, numbers, hyphens | Default connection; `MAPP` resolves from `DBS_MAPP`. |
| `locale.name` | string | Human-readable locale title. |
| `locale.extent.north/south` | number, `-90..90` | Latitude bounds. North must be at least south. |
| `locale.extent.east/west` | number, `-180..180` | Longitude bounds. XYZ v4.23.4 expects east to be at least west. |
| `locale.extent.mask` | boolean | Shades the map outside the configured extent. |
| `locale.view.lat/lng` | latitude/longitude | Initial map centre. |
| `locale.view.z` | number, `0..30` | Initial zoom. |
| `locale.mapviewControls[]` | OpenLayers control names | This file uses `Zoom`; unknown names are ignored by XYZ. |
| `locale.ScaleLine` | `metric`, `imperial` | Scale-line units in XYZ v4.23.4. |
| `locale.layers.<key>` | object | Layer key used in URLs, hooks, and queries. |
| `layer.name` | string | Display label; defaults to the layer key. |
| `layer.display` | boolean | Initial visibility. |
| `layer.format` | XYZ format key | `cluster`, `geojson`, `googleMapTiles`, `mapboxStyle`, `maplibre`, `mvt`, `tiles`, `vector`, or `wkt`. The dashboard currently creates the database and tile formats used by this deployment. |
| `layer.template` | string | Workspace template merged into the layer; may supply the source properties otherwise required by a concrete layer. |
| `layer.attribution` | `{label: URI}` | Credits shown for the layer. |
| `layer.URI` | string | Required for `tiles`; supports tile placeholders such as `{z}/{x}/{y}`. |
| `layer.source` | OpenLayers source name | Tile source; defaults to `OSM`. Use `XYZ` for ordinary URL-template tiles. |
| `layer.projection` | projection string | Tile projection; defaults to `EPSG:3857`. |
| `layer.dbs` | database key | Optional connection override; inherits workspace `dbs`. |
| `layer.table` | `table` or `schema.table` | PostgreSQL relation used for feature queries. |
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
| `icon.color`, `letter` | colour and one character | Required by `markerLetter`. |
| `icon.colorMarker`, `colorDot` | colours | Required by `markerColor`. |
| `icon.url`, `svg` | non-empty string | Custom icon source supported by XYZ. `svg` is a legacy alias normalized to `url` at load time. Platform-managed files live in `instance/public/svg` and use `/instance/svg/<filename>.svg`. |
| `icon` | object or object array | XYZ may render one icon or multiple icons. The dashboard preserves icon arrays but exposes them as read-only outside Advanced layer JSON. |

Extent and view objects may be absent or partial where XYZ composition supplies
the remaining values. Validation checks only the numeric members that are
present and checks cross-field ordering when both sides of a bound exist.

The dashboard's ordinary catalog controls are intended for a concrete
database-backed layer with one `table`, `geom`, and `qID`. External map styles,
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
