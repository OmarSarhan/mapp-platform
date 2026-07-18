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
              bundled or external PostgreSQL/PostGIS
                            ▲
                            │ bundled sample mode only
                    optional one-shot ETL
                            ▲
                     Leeds ArcGIS REST

external config-cli ── HTTPS/bearer token ──> config API
```

Caddy is the only platform service intended to publish host ports. Bundled
PostgreSQL, XYZ, the configuration service, and the browser runner communicate
over private Compose networks; external PostGIS traffic leaves the backend
network for the operator-managed endpoint. The browser runner additionally
joins a dedicated egress
network so the rendered map can fetch external framework, icon, and basemap
assets. It shares the narrow automation network with the configuration
service and Caddy, but does not join the database/backend or public edge
networks and holds no platform credential. Browser navigation uses a guarded,
un-published Caddy listener on port 8081; the `caddy` hostname is denied on the
published HTTP listener.

## Components

| Component | Responsibility | Persistent inputs/state |
| --- | --- | --- |
| PostgreSQL/PostGIS | Application data and spatial indexes; either bundled sample data or an externally managed server | Named PostgreSQL volume in bundled mode; external operator in external mode |
| ETL | Optional sample-data provisioning through bounded ArcGIS reads and idempotent PostGIS loading | `instance/etl/layers.json`; bundled mode through the wrapper |
| XYZ | Map UI, MVT and feature queries | `var/workspace/workspace.json`, `instance/xyz.env`, public assets |
| XYZ preview | Isolated rendering of a pending proposal candidate without changing the public map | `var/preview/workspace.json`, `var/preview-reload`, public assets |
| Configuration service | Dashboard, catalog discovery, validation, proposals, audit, preview publication, reload requests | `var/workspace`, `var/control`, `var/reload`, `var/preview`, `var/preview-reload` |
| Browser runner | Authenticated visual validation with bounded map origin and isolated outbound asset access | `var/control/artifacts` |
| Caddy | TLS, host routing, response headers, upstream file-provider guard | Caddy named volumes |
| Standalone CLI | Remote inspection, proposals, application and verification | State on the separate client computer |

## Filesystem boundary

The versioned `instance` tree contains reviewed deployment inputs:

- `workspace.seed.json` initializes a missing live workspace.
- `xyz.env` contains non-secret XYZ settings.
- `etl/layers.json` selects optional sample ETL sources and fields.
- `public/svg` contains public custom icons.

The ignored `var` tree contains live state:

- `workspace` is the authoritative workspace and its previous atomic-save
  backup.
- `control` contains authentication and device-authorization state, sessions,
  token records, audit entries, proposals, durable operation records, and
  visual artifacts.
- `preview` contains the private candidate workspace used only by
  `xyz-preview`.
- `reload` is a narrow generation/fingerprint channel between the
  configuration service and XYZ supervisor.
- `preview-reload` is a separate generation/fingerprint channel between the
  configuration service and the preview XYZ supervisor.

The control tree contains authentication material and sensitive operational
records. It must not be mounted into XYZ or served as a public static
resource.

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

Proposal preview publishes an integrity-checked stored candidate to a dedicated
`xyz-preview` process with separate workspace and reload state. The live
workspace and live XYZ generation are never changed. A process-wide lock pins
one proposal candidate through browser completion, and artifact metadata binds
the result to its proposal ID and candidate hash.

## Database roles

- `DBS_MAPP` is the single runtime connection shared by XYZ and configuration
  discovery/validation. Its role should have only the read privileges required
  by mapped workspace layers.
- In bundled mode, the bootstrap PostgreSQL role initializes the sample
  database and is not passed to application services. The ETL role owns its
  sample schema and the XYZ role reads it.
- In external mode, roles, PostGIS installation, schema privileges, TLS,
  backup, and recovery are owned by the external database operator. The
  bundled bootstrap and ETL roles are unused.

Managed derived layers deliberately use a separate `DERIVED_DATABASE_URL`.
Only the configuration service receives this narrow identity, which owns
`derived_layers` and reads approved source schemas. XYZ receives only read
access to the results. See [Managed derived layers](derived-layers.md).

## XYZ framework boundary

The XYZ image clones and verifies a pinned upstream tag and full commit during
the image build. The platform does not maintain a fork or edit the framework.
Instance-specific behavior is supplied through the workspace, public assets,
database connection mapping, gateway policy, and child-process supervisor.
