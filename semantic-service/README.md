# MAPP semantic service

The semantic service is a private Python HTTP service backed by PostgreSQL
(`psycopg` in [`requirements.txt`](requirements.txt)), storing the catalog in
the `semantic` schema of the packaged platform database. Its current service
release is recorded in [`VERSION`](VERSION), and its HTTP surface is namespaced
under `/v1`. The public configuration API is the only supported caller; the
service must not be published directly.

Runtime invariants:

- one non-empty internal bearer token is required;
- caller identity and semantic scopes arrive from the configuration service;
- generated facts change only through lifecycle events;
- curated annotations change only through checked, version-bound proposals;
- database access is split across two narrow PostgreSQL roles (supplied by the
  packaged database overlay): `SEMANTIC_DATABASE_URL` (`mapp_semantic`, owning
  only schema `semantic` for writes) and `SEMANTIC_READER_DATABASE_URL`
  (`mapp_semantic_reader`, `USAGE` and `SELECT` on `semantic` only for reads);
  the legacy writable `/state` SQLite mount was removed (historical SQLite
  files are no longer authoritative; see [`../docs/security.md`](../docs/security.md)
  and [`../docs/backup-restore.md`](../docs/backup-restore.md) for backup and
  restore of the `semantic` schema);
- growing read collections support bounded pagination contract `1`.

## Isolated tests (disposable PostgreSQL database required)

The unit/database test suite requires `SEMANTIC_TEST_DATABASE_URL` pointing to
a **disposable scratch PostgreSQL database**. Before every test,
[`tests/database_fixture.py`](tests/database_fixture.py) runs
`DROP SCHEMA IF EXISTS semantic CASCADE` and `CREATE SCHEMA semantic` on the
configured target — **never** point `SEMANTIC_TEST_DATABASE_URL` at a
production or retained catalog database. If `SEMANTIC_TEST_DATABASE_URL` is
unset, database-backed tests are skipped (a skipped run is not passing
validation).

Run from the platform root against a disposable test database (and build the
service image separately if validating packaging):

```sh
SEMANTIC_TEST_DATABASE_URL='postgresql://user:pass@host:5432/mapp_semantic_test?sslmode=disable' \
  PYTHONPATH=semantic-service \
  python -m unittest discover -s semantic-service/tests -v
docker build --tag mapp-semantic-service:test semantic-service
```

The external API/CLI compatibility and pagination declaration lives at
[`contracts/api-compatibility-v1.6.json`](../contracts/api-compatibility-v1.6.json).
