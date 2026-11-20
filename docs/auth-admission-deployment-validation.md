# OMA-125 disposable deployment gate

**Gate failed; OMA-76 remains NOT READY.** Tests ran on 2026-11-20 against
the committed archive `f459a2de844acb75d41fd46069e5f7bd2056d619` without
application source changes. The controller verified all 367 Git blob hashes
and executable modes. No production environment or data was used.

## Environment and lifetime

The user authorized a small test machine for at most one hour. The control
plane provisioned a Hetzner CPX32 in Helsinki: four x86 CPUs, 8 GB RAM and
160 GB disk. Creation was 18:47:19 UTC; deletion was scheduled for 19:46:00
UTC, with no extension. The controller armed independent persistent teardown
before creation; a guest shutdown timer supplies a backstop. Teardown
confirmation is a separate controller event, not implied by this report.

The controller recovered an initial cloud-init boot-command failure through
rescue. Docker 29.1.3, Compose 2.40.3 and source-transfer checks then passed.
Testing began at 18:57 UTC. Only disposable guest configuration was generated;
MCP was enabled with `MAPP_MCP=1`. Gemini remained unconfigured.

## Results

| Check | Actual outcome |
| --- | --- |
| `./bin/mapp init --demo` | Passed |
| First `./bin/mapp all` | Failed: concurrent live/preview XYZ export reported the shared image tag already exists |
| `COMPOSE_PARALLEL_LIMIT=1 ./bin/mapp all` | Passed initial verification; source-content and live MVT checks skipped because data was not yet loaded |
| `./bin/mapp demo --no-semantics` | Failed at Census ingestion |
| `./bin/mapp demo` without a key | Failed at the same Census ingestion |
| Repeated `./bin/mapp demo` | Failed at the same Census ingestion; operational sources reloaded |
| Subsequent `./bin/mapp verify` | Failed: Census relation has zero rows, expected 178605 |
| Actual browser through private SSH tunnel | Initial XYZ/OpenStreetMap rendered; showcase data layers were not created |
| Keyed Gemini | Unrun; no credentials or external generation authorized |

The pinned Census geometry endpoint returned ArcGIS error 499, **Token
Required**:
`https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Output_Areas_2021_EW_BGC_V2/FeatureServer/0`.
The operational Leeds sources loaded. No credentials, replacement dataset or
weakened row-count check were used to get past Census refusal. Four derived
relations, the saved ten-layer workspace and representative showcase rendering
therefore remain unverified. The basemap screenshot is only initial-page
evidence.

## Packaged test results and launcher defects

The literal `COMPOSE_PARALLEL_LIMIT=1 ./bin/mapp test` failed in config
discovery: 1334 tests, two failures, two errors and one skip. The observed
test-environment problems were:

- A route uniqueness test found the broker relative to `/app` rather than
  the mounted repository, so `operations` could not be imported.
- The high-descriptor fixture could not allocate descriptor 1100 under the
  service's file-descriptor limit.
- Config's broker integration fixture lacked Authlib in the config image.
- Two federation unit fixtures inherited a live semantic-service URL instead
  of running with their assumed isolated service configuration.

A separate MCP image run found a cross-service test importing the config
lease helper without the test-only psycopg dependency. Production MCP does
not need a PostgreSQL driver.

Diagnostic runs supplied these fixtures without changing production images,
application source or assertions. Config and broker source paths were explicit;
test containers used `nofile=2048:2048` and isolated service settings. Authlib
and psycopg packages were mounted read-only from the already built service
images. The existing Codex 0.167.0 musl binary and exact CLI workflow blob
`bc3ca233067641044e49b2bc079b8e546fdcf13a` were mounted only for MCP tests.
An initial complete MCP attempt omitted creating its temporary `CODEX_HOME`;
both Codex fixtures exited before initialize. Creating the directory fixed
that harness error.

Final packaged results:

- Config: **1343 passed**, zero skips, 82.005 seconds.
- OAuth: **516 passed**, zero skips, 132.180 seconds.
- MCP: **482 passed**, zero skips, 52.517 seconds, including both actual
  Codex client tests and the source-drift check.

These diagnostic passes do not turn the original launcher into a pass. Its
test-fixture setup still needs repair. The pinned images report Python
3.12.13, psycopg 3.3.4 and libpq 18 with `has_cancel_safe(check=True)` true.
Database-backed tests used the packaged PostgreSQL 17.10/PostGIS/H3 database,
including real cancellation, rollback, reset and role-capacity regressions.

## Evidence and remaining work

Sanitized evidence is retained on the controller at
`/root/.t3/scratch/oma125-vm-20261120/evidence`: `commands.txt`,
`progress.json`, `launcher-failures.txt`, `build-workaround.txt`,
`initial-verify.txt`, the three demo summaries, `verify-after.txt`,
`alpine-driver.txt`, packaged suite summaries and `initial-basemap.png`.
The browser tab and local SSH tunnel were closed. Guest fixture cleanup and
VM deletion must be recorded separately once confirmed.

Required follow-up is the launcher fixture repair and an approved, available
Census source followed by the full demo/browser rerun. The independently
service-blocked OMA-114 review remains unresolved; this deployment exercise
did not replace or retry it. No merge or production deployment was performed.

## Authorized source revision and extended test window

The user subsequently authorized exactly one additional hour, with fixed
teardown at **20:46 UTC**, and replacing the unavailable Census endpoint with
the official public ONS `OA_2021_EW_BGC_V2_RUC` service. The controller updated
both independent expiry mechanisms; this report does not imply deletion.

The replacement is ArcGIS item `ba871c7913184894b9c8dece9a542dfc`, owned by
`ONSGeography_data`, with public access and an Open Government Licence v3
description. It supplies 2021 output-area boundaries joined to rural/urban
classification. The loader imports only OA codes and geometry. This is an
explicit source revision, not proof of byte identity with the former service.

A complete read-only audit on the disposable VM took 246.752 seconds. It
observed 178605 rows and unique England codes, identical start/end counts,
and unchanged metadata across the scan. The canonical metadata-and-feature
SHA-256 was
`f37f647557234c421b298013ab1fe2062dfb6573a6ff4e4a3e72b28e7038885f`.
Both manifests now pin that result. The closed URL allowlist and verifier
expectation changed together; the 47 topic pins, 467 measures, row count and
64-repair ceiling did not change.

The rebuilt ETL image ran `test_census_config`, `test_census_main`,
`test_census_pipeline`, `test_census_database`, `test_arcgis` and `test_nomis`
against an isolated PostGIS test database: **92 passed, zero skips**. The
first run had one fixture error because the `leeds` schema was absent;
creating that schema in the scratch database and rerunning resolved it.
Evidence is in `ons-source-provenance.json`, `ons-source-audit.txt` and
`ons-etl-tests.txt`. Full demo reconciliation and browser checks against this
revision are reported below; these source checks alone do not pass the gate.

### Full load, preview-bound approval and live browser

The new `demo --no-semantics` load passed the geometry hash, all 47 topic
archives and complete OA-code reconciliation. It published 178605 rows and
467 measures, with two geometry repairs within the demo's configured extent.
All four saved derived relations were created and became semantically ready.
Workspace publication then failed with `proposal.preview_required`: the
demo helper sends only `approved: true`, while the current API also requires
exact retained preview bindings. The helper was not changed to bypass this
requirement. This clean-start demo invocation remains a failed gate.

Prepared a proposal-bound comparison in isolated XYZ. The initial viewport
capture had no legend or popup; a separate feature-framed capture with the
styling panel produced complete evidence, with `renderPassed`, `checksPassed`
and `review.complete` true. The user explicitly approved proposal
`1795204645-e8eefb22c57d-0275d1` with preview operation
`47133bb9f67f48dd7e6f5750e28313c2` on the disposable VM only. After rechecking
that exact retained binding, the API apply returned HTTP 200, status `applied`,
and healthy completed reload generation 1. The short-lived apply token was
revoked immediately. No incomplete-preview acknowledgement was used.

API readback exactly matched `docker/demo-sources/workspace-demo.json`, with
ten layers. `./bin/mapp verify` then passed without the previous missing-MVT
skip. Actual browser checks through a private SSH tunnel rendered population
polygons, H3 country-of-birth cells, derived path-length styling, a
country-of-birth polygon layer and source bus-stop icons. The corresponding
MVT requests returned HTTP 200, with no observed browser console errors.
Clicking a bus stop opened Y11 Calverley Street / stop 23462 / Headingley.
Screenshots and the network/interaction ledger are retained as
`ons-live-*.png` and `ons-live-browser.json`; exact workspace readback and
apply/reload results are in `ons-workspace-readback.json` and
`ons-approved-apply.json`. The browser tab and SSH tunnel were closed.

These checks establish the deployed runtime after an explicitly approved
manual completion. They do not repair the clean-start demo helper or the
literal test launcher's fixture failures. Default and repeated no-key
`./bin/mapp demo` invocations both subsequently exited 0. Each reloaded the
public sources, matched the three view definitions, refreshed the H3
materialized relation, read back the saved ten-layer workspace and completed
a fingerprint-bound XYZ reload. The repeated run's SSH connection closed
before completion; its original process continued on the VM, and its durable
exit marker and final log confirmed success without restarting it.

A further complete `--no-semantics` invocation against this approved fixture
also exited 0, followed by a successful final `./bin/mapp verify`. All four
loads against the new pin finished with 178605 rows, 47 topics and two repairs.
Final workspace readback still exactly matched the ten-layer fixture. Both
default runs explicitly reported no `GEMINI_APIKEY` and skipped descriptions;
keyed generation remains unrun.

| Extended-window gate | Result |
| --- | --- |
| Full public ONS source audit and closed manifest pin | Passed |
| Focused ETL suite with real PostGIS | 92 passed, zero skips |
| Clean-start `demo --no-semantics` | Failed at required preview binding after source/derived success |
| Complete preview, explicit approval, exact-bound apply and reload | Passed |
| Default no-key demo and full repeat after approved setup | Both passed |
| Full `demo --no-semantics` rerun after approved setup | Passed |
| Final verifier, ten-layer readback and actual browser checks | Passed |
| Literal test launcher / fresh demo helper / independent mutation review | Still unresolved; overall NOT READY |

The successful seeded-state runs do not erase the clean-start failure. No
approval, resource, revision or source-integrity guard was weakened, and no
production workspace was changed. Teardown confirmation remains owned by
the controller; the fixed deadline is 20:46 UTC.

Extended-window cleanup completed: final workspace equality passed, the
remaining preview token was revoked, the isolated ETL test database was
dropped, and `./bin/mapp down` exited 0. `docker ps -q` then returned no
running containers. Raw private fixture logs/responses were removed after
retaining sanitized results. Evidence remained below 32 MiB total and 8 MiB
per file. Fixture volumes remain only until the controller deletes the VM;
this is service cleanup, not confirmation of VM deletion.
