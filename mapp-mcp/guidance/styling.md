# Mapping a styling request onto workspace properties

"Make the bus stops blue" is not one property. Which property depends on the
layer's geometry and its effective style, and choosing the wrong one produces a
change that applies cleanly and looks like nothing happened.

Inspect geometry and effective style with `layers_get` before selecting a
property.

## Which property carries the colour

- **Filled point symbols** — `dot`, `target`, `triangle`, `square`, `diamond`,
  `semiCircle` — use `style.default.icon.fillColor`.
- **`circle` points** use `style.default.icon.strokeColor`.
- **A `markerLetter` icon** uses `style.default.icon.color` for the outer pin
  and `style.default.icon.letter` for the centre text. Both must sit inside
  `icon`. `fillColor` does not recolour this symbol.
- **`markerColor`** has an outer `colorMarker` and an inner `colorDot`. Ask
  which one is meant when the request does not say.
- **Custom SVG files** generally cannot be recoloured by a workspace colour.
  Say so rather than making a change that does nothing.
- **Lines** use `style.default.strokeColor`.
- **Polygons** — an unqualified colour means `style.default.fillColor`; an
  outline colour means `style.default.strokeColor`.

## States are independent

Default, highlight, selected, hover, label and theme styles are separate.
Change only the state that was asked for.

**Do not map the word "hover" from its spelling.** `style.highlight` supplies
the visual pointer highlight. A layer `hover` object is field-driven
interaction configuration and may require a catalogue column. They are
different things with similar names; inspect the schema and the effective style
before choosing.

## Themes override defaults

A theme- or feature-driven style overrides a simple default. Changing
`style.default` alone does not recolour a themed polygon — every graduated
category owns its own `fillColor`, `strokeColor`, `fillOpacity` and
`strokeOpacity`.

Explain that limitation rather than claiming the change affects every feature.

## Graduated metric layers

**Keep three names separate**: the workspace layer key, the visible
`layer.name`, and the backing relation. Use a stable ASCII-safe key — say
`Passport_holders_United_Kingdom` — for JSON Pointer paths and layer
activation, and keep spaces and punctuation in the visible name. A
display-formatted key does not bind reliably when a newly grouped layer is
activated.

**Style and filter on the raw numeric field**, never on a formatted text field.
For a percentage shown to one decimal place, a value displaying as `0.0%` is
not zero — the equivalent raw predicate is `metric_percent >= 0.05`. Apply it
in the layer's fixed default filter, and apply it to every related layer in the
group.

**Audit the cutoffs before proposing them.** `layers_statistics` returns
bounded category counts for a field; pass the exact proposed breaks to get
candidate class counts and inclusive-bound flags. The response contains no raw
rows. A truncated category-value result is not distribution evidence.

**Give each metric its own complete ramp.** For a group of mutually exclusive
metrics, a full white-to-hue ramp per layer reads correctly; reusing shared
intermediate tints with only the final colour differing does not.
