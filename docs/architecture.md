# Architecture

## Scope

MAPP Platform owns the live server and its configuration API. The standalone
`config-cli` is an external client and should be released, installed, and
secured independently.

```text
                         public network
                              │
                              ▼
                       Caddy 80 / 443
                         │          │
                  map hostname   config hostname
                         │          │
                         ▼          ▼
                        XYZ     config dashboard/API
                         │       │       │
                         └──┬────┘       └── browser runner
                            ▼
                     PostgreSQL/PostGIS
                            ▲
                            │
                      one-shot ETL
                            ▲
                     Leeds ArcGIS REST

external config-cli ── HTTPS/bearer token ──> config API
```

Caddy is the only service intended to publish host ports. PostgreSQL, XYZ, the
configuration service, and the browser runner communicate over private
Compose networks. The browser runner additionally joins a dedicated egress
network so the rendered map can fetch external framework, icon, and basemap
assets. It shares the narrow automation network with the configuration
service and Caddy, but does not join the database/backend or public edge
networks and holds no platform credential. Browser navigation uses a guarded,
un-published Caddy listener on port 8081; the `caddy` hostname is denied on the
published HTTP listener.

## Components

| Component | Responsibility | Persistent inputs/state |
| --- | --- | --- |
| PostgreSQL/PostGIS | Application data, spatial indexes, ETL control tables | Named PostgreSQL volume |
| ETL | Bounded ArcGIS reads and idempotent PostGIS loading | `instance/etl/layers.json` |
| XYZ | Map UI, MVT and feature queries | `var/workspace/workspace.json`, `instance/xyz.env`, public assets |
| Configuration service | Dashboard, catalog discovery, validation, proposals, audit, reload requests | `var/workspace`, `var/control`, `var/reload` |
| Browser runner | Authenticated visual validation with bounded map origin and isolated outbound asset access | `var/control/artifacts` |
| Caddy | TLS, host routing, response headers, upstream file-provider guard | Caddy named volumes |
| Standalone CLI | Remote inspection, proposals, application and verification | State on the separate client computer |

## Filesystem boundary

The versioned `instance` tree contains reviewed deployment inputs:

- `workspace.seed.json` initializes a missing live workspace.
- `xyz.env` contains non-secret XYZ settings.
- `etl/layers.json` selects ETL sources and fields.
- `public/svg` contains public custom icons.

The ignored `var` tree contains live state:

- `workspace` is the authoritative workspace and its previous atomic-save
  backup.
- `control` contains authentication hashes, sessions, token records, audit
  entries, proposals, and visual artifacts.
- `reload` is a narrow generation/fingerprint channel between the
  configuration service and XYZ supervisor.

The control tree is sensitive even though raw passwords and bearer tokens are
hashed. It must not be mounted into XYZ or served as a public static resource.

## Configuration flow

1. A dashboard administrator or remote client reads the current workspace and
   revision.
2. The configuration service validates requested JSON Pointer operations
   against the schema, live database catalog, expression rules, and render
   probe.
3. A proposal records its original revision, candidate, focused diff,
   explanation, warnings, and actor.
4. Applying a pending proposal performs a revision-bound atomic save.
5. The service requests an XYZ generation reload and waits for the expected
   workspace fingerprint.
6. A visual test selects a data-aware view and records a report and
   screenshots.

Request parsing uses strict JSON and strict RFC 6901 pointer traversal. The
top-level locale remains the default; effective named locales use XYZ's
framework-specific composition rules. When the raw default is absent, XYZ
synthesizes an empty default instead of selecting a named locale. Concrete
database layers receive live catalog and render probes, while valid external,
template, inline-feature, and zoom-keyed sources are preserved without
pretending that one database relation represents them.

Proposal preview that temporarily substitutes a candidate into the public
workspace is not a sufficiently isolated production design. A dedicated
preview process or namespace remains required.

## Database roles

- The bootstrap PostgreSQL role initializes the database and is not passed to
  application services.
- The ETL role owns its target schema and writes imported data.
- The XYZ role has read-only access and is shared with configuration discovery
  and validation so that a configuration cannot rely on privileges XYZ lacks.

## XYZ framework boundary

The XYZ image clones and verifies a pinned upstream tag and full commit during
the image build. The platform does not maintain a fork or edit the framework.
Instance-specific behavior is supplied through the workspace, public assets,
database connection mapping, gateway policy, and child-process supervisor.
