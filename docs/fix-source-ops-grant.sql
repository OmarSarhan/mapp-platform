-- Restore the read grants on the federated `source_ops` schema.
--
-- Why this is needed
-- ------------------
-- `locale.layers.Bus_Stops.tables."15"` reads `source_ops.bus_stops`. The
-- schema, its foreign server (`ops_srv` -> ops-db) and its foreign tables all
-- exist and query correctly -- reading it as `mapp_federation` returns 4,233
-- rows. What is missing is the grant step: `source_ops` is owned by
-- `mapp_federation` and its ACL names only that role, so the read-only
-- connection the platform validates through (`DBS_MAPP`, connecting as
-- `mapp_xyz`) can neither USAGE the schema nor SELECT the table.
--
-- The effect is larger than one layer. `validate_catalog` builds its index
-- from relations the read-only connection can select, so every workspace
-- candidate fails with:
--
--     locale.layers.Bus_Stops.tables.15:
--     Table is not selectable through the configured read-only connection.
--
-- which refuses `proposals.check`, `proposals.create` and `proposals.apply`
-- alike. No agent can propose or apply anything on this instance until it is
-- fixed. It is also why Phase 1 wave 6 could be proved as far as
-- authorisation and no further: `proposals_apply` reached the platform
-- holding a valid receipt and was then refused on this rule.
--
-- What this does
-- --------------
-- Exactly the two statements `federation_store.py` issues at provisioning
-- time (see the `GRANT USAGE ON SCHEMA` / `GRANT SELECT ON ALL TABLES` pair
-- in `_provision`), to exactly the two roles it names there:
-- `DERIVED_OWNER_ROLE` (mapp_derived) and `DERIVED_READER_ROLE` (mapp_xyz).
-- Nothing else. It grants no write, creates no role, and touches no other
-- schema -- `source_census` is left alone because no layer reads it.
--
-- How to run it
-- -------------
--   docker compose exec -T db psql "$FEDERATION_DATABASE_URL" \
--     -f /dev/stdin < docs/fix-source-ops-grant.sql
--
-- or from the config-ui container, which already holds the credential:
--
--   docker compose exec -T config-ui sh -c \
--     'psql "$FEDERATION_DATABASE_URL"' < docs/fix-source-ops-grant.sql
--
-- Run as `mapp_federation`, which owns the schema. `FEDERATION_DATABASE_URL`
-- already connects as that role.
--
-- The better fix, and why this exists instead
-- -------------------------------------------
-- The platform's own path is `POST /api/federation/aliases/ops/provision`,
-- which issues these grants as part of a full observe-and-rebind and leaves
-- the registry consistent. Both aliases currently read `status: unavailable`,
-- so that path would re-observe and re-bind foreign tables that are working
-- today -- a heavier and more consequential operation, needing an operator
-- credential and three explicit acknowledgements. This restores the one
-- missing step without disturbing anything that works. If the registry's
-- `unavailable` status matters for other reasons, re-provision properly
-- instead of running this.

BEGIN;

GRANT USAGE ON SCHEMA source_ops TO mapp_derived;
GRANT SELECT ON ALL TABLES IN SCHEMA source_ops TO mapp_derived;

GRANT USAGE ON SCHEMA source_ops TO mapp_xyz;
GRANT SELECT ON ALL TABLES IN SCHEMA source_ops TO mapp_xyz;

COMMIT;

-- Verify, as the role that actually reads:
--
--   SELECT has_schema_privilege('mapp_xyz', 'source_ops', 'USAGE'),
--          has_table_privilege('mapp_xyz', 'source_ops.bus_stops', 'SELECT');
--
-- Both must be true. After that, `proposals_check` through the MCP surface
-- should return a `checkFingerprint` rather than a validation error.
