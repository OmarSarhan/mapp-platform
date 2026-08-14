# MAPP semantic service

The semantic service is a private, standard-library Python HTTP service backed
by SQLite. Its current service release is recorded in [`VERSION`](VERSION),
and its HTTP surface is namespaced under `/v1`. The public configuration API is
the only supported caller; the service must not be published directly.

Runtime invariants:

- one non-empty internal bearer token is required;
- caller identity and semantic scopes arrive from the configuration service;
- generated facts change only through lifecycle events;
- curated annotations change only through checked, version-bound proposals;
- SQLite state is stored in the private `/state` mount with restrictive modes;
- growing read collections support bounded pagination contract `1`.

Run its isolated tests from the platform root:

```sh
PYTHONPATH=semantic-service \
  python -m unittest discover -s semantic-service/tests -v
docker build --tag mapp-semantic-service:test semantic-service
```

The platform CI runs both commands. The external API/CLI compatibility and
pagination declaration lives at
[`contracts/api-compatibility-v1.5.json`](../contracts/api-compatibility-v1.5.json).
