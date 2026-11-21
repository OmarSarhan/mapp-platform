# XYZ v5 runtime

Status: local implementation and compatibility checks passed. Deployment
acceptance against the target environment remains required.

The deployment pins upstream [v5.0.9](https://github.com/GEOLYTIX/xyz/releases/tag/v5.0.9)
to commit `209f43eb2e7587f6d1e7b93296bae19922c56034` (the peeled annotated tag,
verified from an upstream clone). Upstream recommends this release for the
location/layer SQL-injection fixes; it also fixes idle session renewal and
location removal races. This is the latest published release inspected on
2026-11-21, not a claim of an upstream long-term-support policy.

The v5 monorepo moves the server to `apps/xyz/server.js` and database pool
implementation to `apps/xyz/mod/utils/dbs.js`. The image verifies the same
20-connection ceiling. It uses upstream pnpm 11.3.0 and the `@geolytix/mapp`
browser build. Upstream supplies no dependency lockfile. The root working
directory, workspace and public asset mounts remain unchanged. Environment
hydration supports ordinary process environment without a Varlock secret
manager. No upstream source is patched or vendored.

Empty locale override arrays now retain inherited arrays. Server planning and
dashboard composition follow that upstream merge behavior. Nonempty subsets
still replace and other arrays concatenate. Existing closed schema fields
remain the conservative v4-audited subset; this upgrade does not authorize
new workspace properties. Historical field-audit links retain their original
revision and are not a claim of a complete v5 field audit.

The two source-controlled external plugin manifests now declare exactly
`=5.0.9`, supported by the isolated candidate preview described below. Future
runtime versions require renewed compatibility evidence. Roll back the
deployment inputs and manifests together if reverting to v4.
No live workspace migration is performed by this change. Existing deployments
with explicit XYZ environment pins must update the ref, full commit and image
tag together during an independently approved deployment.

Deployment acceptance still requires healthy services, representative database
data, reload verification and candidate visual evidence, including any external
plugins. Run `./bin/mapp verify` in that environment before promoting v5.

## Local verification

The Docker image build passed. A network-disabled, read-only container started
the supervisor with the seed, served `/public/js/lib/mapp.js`, and reported
healthy generations 0 and 1 after a reload request. This exercises restart
coordination, not database-backed map acceptance.

Final local checks:

- `python -m unittest discover -s scripts/tests`: 239 run, 29 skipped, passed.
- `PYTHONPATH=config-ui python -m unittest discover -s config-ui/tests -p
  'test_*schema.py'`: 130 run, 36 skipped, passed.
- Control API suite with `CONTROL_TEST_DATABASE_URL` pointing to disposable
  PostgreSQL 17 over a local Unix socket: all 65 passed, no skips.
- Plugin registry: 3 passed. Dashboard API Node tests: 7 passed.
- Dashboard Vitest: all 161 passed with `--maxWorkers=1`. The initial parallel
  run had four 5-second timeouts; the serial rerun required no code changes.
- `docker compose --env-file .env.example config --quiet` and
  `git diff --check`: passed.

Python checks used a temporary virtual environment containing the repository's
declared dependencies. The `mapp test`, `doctor`, `config`, and `verify` wrappers
still require a configured `.env`; a target deployment was not initialized.
Skipped tests and full deployment verification remain environment acceptance
work, not implied passes.

## Isolated plugin candidate preview

`browser-runner/tests/xyz-runtime-plugins.mjs` loads the actual browser assets
extracted from the built v5 image, OpenLayers 10.8.0 (the upstream view's pinned
version), and repository plugin modules in Chromium. Requests are intercepted;
no live services or database are contacted. Synthetic tile requests fail once
with HTTP 503 and then succeed. Count queries return controlled counts 3 and 7.
The test checks actual upstream layer decoration, drawer insertion, visibility,
hide/show count refresh, successful bounded tile retry, and absence of page
errors. All eight requested raster tiles succeeded on their second attempt.
The screenshot was inspected and retained under ignored
`var/evidence/xyz-v5/plugins.png`. It shows the visible count badge in the
upstream drawer; the intentionally blank raster fixture is not geographic data.

To repeat, install `browser-runner` dependencies and its Chromium browser,
extract `/app/xyz/public` from the built image into a temporary asset directory,
and put the upstream OpenLayers `dist/ol.js` there as `ol.js`. Run:

```sh
XYZ_PREVIEW_ASSETS=/path/to/assets node browser-runner/tests/xyz-runtime-plugins.mjs
```

The preview is bounded browser integration evidence. It does not replace
target-database, MVT rendering, complete-map visual acceptance, or production
reload verification. Existing tile-retry component tests also passed as part
of the dashboard suite.
