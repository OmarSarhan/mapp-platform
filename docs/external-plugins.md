# External XYZ plugins

External plugins are reviewed deployment code stored under
`instance/public/plugins`. They are not copied into or applied as patches to
the pinned GEOLYTIX XYZ checkout. The same read-only directory is mounted into
live XYZ, preview XYZ, and the configuration service.

## Package format

Each plugin has its own directory containing `plugin.json`, one `.mjs` or `.js`
entry point, and optional contained assets. Discovery reads the manifest and
hashes files; it never imports the module.

The manifest declares its stable ID, semantic version, compatible XYZ version
range, entry file, `mapp.plugins` registration key, locale/layer scope,
dispatch modes, configuration property and closed JSON Schema, dependencies,
prerequisites, documentation, aliases, and declarative preview assertions.
The viewport layer-count package is the reference implementation.

Modules execute as trusted same-origin browser JavaScript. They must register
their function as a side effect on `mapp.plugins`; XYZ ignores module exports.
Installation and code changes require source review and a deployment. The
dashboard does not upload executable code.

## Validation and lifecycle

The server rejects unregistered and remote URLs, wrong-scope configuration,
missing module/configuration pairs, undeclared synchronous dispatch, missing
dependencies, and properties outside each plugin schema. Dependencies are
never installed or reordered automatically.

The catalogue fingerprint covers normalized manifests and entry hashes.
Proposals and preview evidence bind to it. A plugin change makes earlier
proposals and previews stale, requiring a new proposal and candidate preview.

Preview assertions use platform-owned checks: registration, locale/layer
dispatch with an observable selector, selector existence or visibility, and
absence of plugin-related browser errors. Manifest-provided test code is never
executed by the configuration service. A post-apply visual test separately
confirms behavior against live XYZ.

Authenticated callers with `inspect` can read `GET /api/plugins`. The response
contains the pinned built-in registry, source-controlled external manifests,
workspace usage and diagnostics, declarative preview checks, and the catalogue
fingerprint. The standalone client exposes that same server-owned response
through `config-cli plugins list`, `show`, `validate`, and `usage`; these are
inspection commands and never install, upload, import, or mutate a plugin.
Treat an absent advertised plugin command as unavailable rather than calling a
similar route directly.

`./bin/mapp doctor` validates source manifests; `./bin/mapp verify`
additionally compares mounted plugin hashes across services.
