# Validation log

This is a bounded acceptance log inspired by the requested autoresearch
workflow. `keep` means the change met its gate; `blocked` means the gate could
not complete because of an environmental or external dependency, not that it
passed.

The 2026-07-15 entries predate the platform/CLI directory split and the
`instance`/`var` state boundary. They are historical evidence only. Later rows
state explicitly which final split paths and runtime checks passed; no row
establishes unlisted production, backup, or restore acceptance.

| Date (UTC) | Change or baseline | Gate | Result | Evidence |
| --- | --- | --- | --- | --- |
| 2026-07-15 | Pin XYZ v4.23.4 | Tag resolves to expected full commit | keep | `a6f03c07dd7aaae2e9ab04087143ee0400e15cb9`; Node 22+ requirement and build command checked in upstream source/wiki |
| 2026-07-15 | Build pinned XYZ and load mounted workspace | Declared pnpm build succeeds; XYZ resolves all instance layers | keep | Node 22.23.1 + pnpm 10.10.0 build passed; local XYZ returned HTTP 200 and resolved OSM plus all three MVT layers with `DBS_MAPP` |
| 2026-07-15 | Select Leeds sample | Point, line, polygon plus varied field types; bounded layer set | keep | live metadata/count/GeoJSON checks: 4,233 bus stops, 2,484 paths, 275 recent planning records |
| 2026-07-15 | ETL transforms and consistency logic | Unit and SQL-composition suite | keep | 21 tests passed in a fresh environment; count drift, duplicate IDs, and concurrent runs skip reconciliation |
| 2026-07-15 | ETL concurrency | Overlapping same-layer runs cannot reconcile each other's rows | keep | two live PostgreSQL sessions confirmed the second lock is rejected until the first releases; the competing pipeline exits before any run record or data write |
| 2026-07-15 | Pin ETL runtime | Exact Python and database-driver releases | keep | ETL image uses `python:3.12.13-slim-bookworm`; `psycopg[binary]` is pinned to the tested 3.3.4 release |
| 2026-07-15 | Leeds source contract | Opt-in live suite and `--check-source` | keep | all three layers reported correct metadata, EPSG:27700, GeoJSON support, and expected geometry families |
| 2026-07-15 | ETL database integration | Two complete least-privilege PostGIS imports | keep | 6,992 rows per run, zero invalid geometries, correct 4326/3857 SRIDs, six GiST indexes, zero deletions or changed-hash timestamps on identical rerun (PostgreSQL 15/PostGIS 3.3 harness) |
| 2026-07-15 | XYZ/PostGIS integration | Load all layers with ETL role, query with read-only XYZ role | keep | local PostgreSQL/PostGIS harness loaded 4,233 + 2,484 + 275 rows; XYZ returned HTTP 200 and a 38,183-byte Bus Stops MVT |
| 2026-07-15 | Compose topology | Parse and normalize with Docker Compose v5.3.1 | keep | base file and optional DB-port override both passed `config --quiet` |
| 2026-07-15 | Rebuild/recreate behavior | Framework and ETL upgrades do not leave stale processes | superseded | XYZ now uses a generation-based child-process supervisor; Caddy remains stable during workspace reloads |
| 2026-07-15 | Acceptance timing | Fresh containers can reach healthy without a false-negative race | keep | verifier polls each required service for up to 60 seconds and still fails immediately on a terminal container state |
| 2026-07-15 | Database initialization/readiness | Least-privilege bootstrap and full initialization health gate | keep | init script created both roles and the ETL-owned schema in a fresh PostgreSQL harness; health now waits for PostGIS, roles, schema, and runtime marker |
| 2026-07-15 | Full container startup | Build images, load PostGIS, rerun ETL, query XYZ | superseded | that execution environment lacked Docker access; the successful 2026-07-16 split-stack run below supersedes this historical limitation |
| 2026-07-16 | Final split component suites | Platform, CLI, packaging, typing, frontend, dependency, syntax, and Compose checks | keep | platform: 59 configuration tests, 23 ETL tests with 1 opt-in live test skipped, 8 wrapper/production tests with 1 host-Node comparison skipped, 4 frontend tests/build, npm audit with 0 findings, and Compose validation; CLI: 62 tests, mypy clean, compilation, wheel/sdist build, `twine check`, and fresh wheel install |
| 2026-07-16 | Browser-runner capacity gate | Bound simultaneous Chromium work, reject excess work, and release the slot | keep | black-box test held the only configured slot, received HTTP 429 for a competing run, then confirmed a later run was accepted after slot release |
| 2026-07-16 | Live split stack | Rebuild, service health, database, guards, workspace identity, reload, and XYZ child topology | keep | `db`, `xyz`, `config-ui`, `browser-runner`, and `caddy` healthy; `./bin/mapp verify` passed; 4,233 bus stops, 2,484 paths, and 275 planning rows retained; live, seed, and backup workspace SHA-256 matched; reload generation completed healthy with exactly one XYZ application child |
| 2026-07-16 | Standalone CLI acceptance | Temporary token, remote inspection, dry run, XYZ status, and authenticated visual test | keep | CLI reported the expected instance/revision and Bus Stops layer, dry run explicitly returned `saved: false`, XYZ was healthy, Chromium visual test passed, and the temporary token was revoked |
| 2026-07-16 | Leeds live source recheck | Metadata, count, and GeoJSON query for every configured ETL layer | blocked | bus and path sources passed; `Planning/MapServer/1` returned ArcGIS error 400 `Failed to execute query`; ETL safety logic retained the existing 275-row snapshot and no alternative planning layer was substituted |

The final local checks above do not replace production acceptance. Record
resolved image digests, production Postgres/PostGIS versions, ETL durations,
first/second-run counts, MVT smoke results, TLS behavior, and restore evidence
when the release is exercised on its intended infrastructure.
