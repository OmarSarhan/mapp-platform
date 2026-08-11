-- Enables cross-database federation testing (docs/federation-architecture-waypoint.md):
-- one explicit postgres_fdw source, provisioned on demand by
-- config-ui/federation_store.py's FederationAliasStore.provision(). Installing
-- the extension and granting FDW USAGE here is a one-time, superuser-only step;
-- CREATE SERVER/CREATE USER MAPPING/IMPORT FOREIGN SCHEMA happen later, per
-- alias, under the derived-owner role.
CREATE EXTENSION IF NOT EXISTS postgres_fdw;
GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO mapp_derived;
