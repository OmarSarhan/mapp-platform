from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DatabaseAccessContractTests(unittest.TestCase):
    @staticmethod
    def normalized(relative_path: str) -> str:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        return re.sub(r"\s+", " ", source)

    def assert_resource_role_defaults(self, relative_path: str) -> None:
        source = self.normalized(relative_path)
        for contract in (
            'ALTER ROLE :"xyz_db_user" CONNECTION LIMIT 32;',
            'ALTER ROLE :"derived_db_user" CONNECTION LIMIT 4;',
            'ALTER ROLE :"derived_db_user" SET search_path = pg_catalog, public;',
            'ALTER ROLE :"xyz_db_user" SET work_mem = \'8MB\';',
            'ALTER ROLE :"xyz_db_user" SET temp_file_limit = \'256MB\';',
            'ALTER ROLE :"xyz_db_user" SET statement_timeout = \'15s\';',
            'ALTER ROLE :"xyz_db_user" SET transaction_timeout = \'30s\';',
            'ALTER ROLE :"derived_db_user" SET work_mem = \'16MB\';',
            'ALTER ROLE :"derived_db_user" SET hash_mem_multiplier = \'1\';',
            'ALTER ROLE :"derived_db_user" SET maintenance_work_mem = \'64MB\';',
            'ALTER ROLE :"derived_db_user" SET max_parallel_workers_per_gather = \'2\';',
            'ALTER ROLE :"derived_db_user" SET temp_file_limit = \'1GB\';',
            'ALTER ROLE :"derived_db_user" SET statement_timeout = \'30min\';',
            'ALTER ROLE :"derived_db_user" SET transaction_timeout = \'35min\';',
            'ALTER ROLE :"derived_db_user" SET lock_timeout = \'5s\';',
        ):
            with self.subTest(path=relative_path, contract=contract):
                self.assertIn(contract, source)

    def assert_federation_role_defaults(self, relative_path: str) -> None:
        source = self.normalized(relative_path)
        for contract in (
            'ALTER ROLE :"federation_db_user" CONNECTION LIMIT 4;',
            'ALTER ROLE :"federation_db_user" SET search_path = pg_catalog, public;',
            'ALTER ROLE :"federation_db_user" SET work_mem = \'16MB\';',
            'ALTER ROLE :"federation_db_user" SET hash_mem_multiplier = \'1\';',
            'ALTER ROLE :"federation_db_user" SET maintenance_work_mem = \'64MB\';',
            'ALTER ROLE :"federation_db_user" SET max_parallel_workers_per_gather = \'2\';',
            'ALTER ROLE :"federation_db_user" SET temp_file_limit = \'1GB\';',
            'ALTER ROLE :"federation_db_user" SET statement_timeout = \'30min\';',
            'ALTER ROLE :"federation_db_user" SET transaction_timeout = \'35min\';',
            'ALTER ROLE :"federation_db_user" SET lock_timeout = \'5s\';',
        ):
            with self.subTest(path=relative_path, contract=contract):
                self.assertIn(contract, source)

    def assert_h3_sql_wrapper_hardening(self, relative_path: str) -> None:
        source = self.normalized(relative_path)
        for contract in (
            "extension_membership.classid = "
            "'pg_catalog.pg_proc'::pg_catalog.regclass",
            "extension_membership.deptype = 'e'",
            "extension.extname = 'h3_postgis'",
            "extension.extnamespace = routine.pronamespace",
            "routine_namespace.nspname = 'public'",
            "public.h3_polygon_to_cells(public.geometry,pg_catalog.int4)",
            "public.h3_polygon_to_cells(public.geography,pg_catalog.int4)",
            "public.h3_polygon_to_cells_experimental(public.geometry,pg_catalog.int4,pg_catalog.text)",
            "public.h3_polygon_to_cells_experimental(public.geography,pg_catalog.int4,pg_catalog.text)",
            "public.h3_lat_lng_to_cell(public.geometry,pg_catalog.int4)",
            "public.h3_lat_lng_to_cell(public.geography,pg_catalog.int4)",
            "public.h3_latlng_to_cell(public.geometry,pg_catalog.int4)",
            "public.h3_latlng_to_cell(public.geography,pg_catalog.int4)",
            "public.h3_cell_to_geometry(public.h3index)",
            "public.h3_cell_to_geography(public.h3index)",
            "public.h3_cell_to_boundary_geometry(public.h3index)",
            "public.h3_cell_to_boundary_geography(public.h3index)",
            "public.h3_cell_to_boundary_geometry(public.h3index,pg_catalog.bool)",
            "public.h3_cell_to_boundary_geography(public.h3index,pg_catalog.bool)",
            "public.h3_cells_to_multi_polygon_geometry(public.h3index[])",
            "public.h3_cells_to_multi_polygon_geography(public.h3index[])",
            "ALTER FUNCTION %s SET search_path = pg_catalog, public",
            "IF hardened_count <> 16 THEN",
        ):
            with self.subTest(path=relative_path, contract=contract):
                self.assertIn(contract, source)

    def test_fresh_h3_install_hardens_catalog_owned_sql_wrappers(self) -> None:
        self.assert_h3_sql_wrapper_hardening(
            "docker/postgis/init/05-h3.sql"
        )

    def test_wrappers_reject_every_database_environment_override(self) -> None:
        expected = {
            "ETL_DATABASE_URL",
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "ETL_DB_USER",
            "ETL_DB_PASSWORD",
            "XYZ_DB_USER",
            "XYZ_DB_PASSWORD",
            "DERIVED_DB_USER",
            "DERIVED_DB_PASSWORD",
            "DERIVED_DATABASE_URL",
            "DERIVED_OWNER_ROLE",
            "DERIVED_READER_ROLE",
            "FEDERATION_DB_USER",
            "FEDERATION_DB_PASSWORD",
            "FEDERATION_DATABASE_URL",
        }
        for relative_path in ("bin/mapp", "scripts/verify.sh"):
            with self.subTest(path=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                start = source.index("reject_database_environment_overrides()")
                end = source.index("\n}", start)
                guard = source[start:end]
            self.assertIn(
                '"${!DBS_@}"',
                guard,
                "every DBS_<ALIAS> variable must be covered by a prefix rule, "
                "not a literal DBS_MAPP enumeration",
            )
            self.assertIn(
                '"${!FEDERATION_DBS_@}"',
                guard,
                "federation connection variables must be covered by their "
                "own prefix rule",
            )
            for key in expected:
                self.assertIn(key, guard)
                self.assertIn('unset "${key}"', guard)

    def test_federation_seed_pins_collatable_columns_to_portable_c(self) -> None:
        source = self.normalized("docker/source-db/seed.sh")
        for contract in (
            "'leeds.smoke_control_orders'::pg_catalog.regclass",
            "a.attcollation <> 0",
            "COLLATE pg_catalog.\"C\"",
            "(n.nspname, co.collname) <> ('pg_catalog', 'C')",
            "seeded collatable columns must use pg_catalog.C",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

    def test_layer_drop_guard_installs_blocking_sql_objects(self) -> None:
        source = self.normalized("docker/postgis/init/25-platform-layer-drop-guard.sql")
        for contract in (
            "CREATE TABLE IF NOT EXISTS public.mapp_platform_layer_dependencies",
            "CREATE OR REPLACE FUNCTION public.mapp_sync_platform_layer_dependencies",
            "CREATE OR REPLACE FUNCTION public.mapp_block_platform_layer_drops()",
            "DROP EVENT TRIGGER IF EXISTS mapp_block_platform_layer_drops",
            "CREATE EVENT TRIGGER mapp_block_platform_layer_drops",
            "ERRCODE = '55006'",
            "DROP TABLE",
            "DROP VIEW",
            "DROP MATERIALIZED VIEW",
            "GRANT EXECUTE ON FUNCTION public.mapp_sync_platform_layer_dependencies",
            "cmd.schema_name",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

    def test_upgrade_includes_layer_drop_guard_migration(self) -> None:
        source = self.normalized("docker/postgis/upgrade-derived.sh")
        for contract in (
            "public.mapp_platform_layer_dependencies",
            "public.mapp_sync_platform_layer_dependencies",
            "mapp_block_platform_layer_drops",
            "DROP EVENT TRIGGER IF EXISTS mapp_block_platform_layer_drops",
            "CREATE EVENT TRIGGER mapp_block_platform_layer_drops",
            "ERRCODE = '55006'",
            "cmd.schema_name",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

    def test_matching_export_cannot_shadow_env_file_interpolation(self) -> None:
        raw_dbs = (
            "postgresql://${XYZ_DB_USER}:${XYZ_DB_PASSWORD}"
            "@db:5432/${POSTGRES_DB}"
        )
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(f"DBS_MAPP={raw_dbs}\n", encoding="utf-8")
            for relative_path in ("bin/mapp", "scripts/verify.sh"):
                with self.subTest(path=relative_path):
                    source = (ROOT / relative_path).read_text(encoding="utf-8")
                    dotenv_start = source.index("dotenv_value()")
                    dotenv_end = source.index("\n}", dotenv_start) + 2
                    guard_start = source.index(
                        "reject_database_environment_overrides()"
                    )
                    guard_end = source.index("\n}", guard_start) + 2
                    script = "\n".join((
                        'ENV_FILE="$1"',
                        source[dotenv_start:dotenv_end],
                        source[guard_start:guard_end],
                        "reject_database_environment_overrides",
                        "[[ ! -v DBS_MAPP ]]",
                    ))
                    subprocess.run(
                        ["bash", "-c", script, "test", str(env_file)],
                        check=True,
                        env={"PATH": os.environ["PATH"], "DBS_MAPP": raw_dbs},
                        capture_output=True,
                        text=True,
                    )

    def test_prefix_rule_covers_a_second_alias_beyond_dbs_mapp(self) -> None:
        raw_dbs = "postgresql://leeds-reader@db:5432/leeds"
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(f"DBS_LEEDS={raw_dbs}\n", encoding="utf-8")
            for relative_path in ("bin/mapp", "scripts/verify.sh"):
                with self.subTest(path=relative_path):
                    source = (ROOT / relative_path).read_text(encoding="utf-8")
                    dotenv_start = source.index("dotenv_value()")
                    dotenv_end = source.index("\n}", dotenv_start) + 2
                    guard_start = source.index(
                        "reject_database_environment_overrides()"
                    )
                    guard_end = source.index("\n}", guard_start) + 2
                    script = "\n".join((
                        'ENV_FILE="$1"',
                        source[dotenv_start:dotenv_end],
                        source[guard_start:guard_end],
                        "reject_database_environment_overrides",
                        "[[ ! -v DBS_LEEDS ]]",
                    ))
                    subprocess.run(
                        ["bash", "-c", script, "test", str(env_file)],
                        check=True,
                        env={"PATH": os.environ["PATH"], "DBS_LEEDS": raw_dbs},
                        capture_output=True,
                        text=True,
                    )

    def test_prefix_rule_rejects_a_mismatched_second_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DBS_LEEDS=postgresql://leeds-reader@db:5432/leeds\n",
                encoding="utf-8",
            )
            for relative_path in ("bin/mapp", "scripts/verify.sh"):
                with self.subTest(path=relative_path):
                    source = (ROOT / relative_path).read_text(encoding="utf-8")
                    dotenv_start = source.index("dotenv_value()")
                    dotenv_end = source.index("\n}", dotenv_start) + 2
                    guard_start = source.index(
                        "reject_database_environment_overrides()"
                    )
                    guard_end = source.index("\n}", guard_start) + 2
                    script = "\n".join((
                        'ENV_FILE="$1"',
                        source[dotenv_start:dotenv_end],
                        source[guard_start:guard_end],
                        "reject_database_environment_overrides",
                    ))
                    result = subprocess.run(
                        ["bash", "-c", script, "test", str(env_file)],
                        env={
                            "PATH": os.environ["PATH"],
                            "DBS_LEEDS": (
                                "postgresql://someone-else@elsewhere:5432/other"
                            ),
                        },
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(2, result.returncode)
                    self.assertIn("DBS_LEEDS", result.stderr)
                    self.assertIn(
                        "conflicts with the authoritative value", result.stderr
                    )

    def test_new_database_reader_has_only_table_select_defaults(self) -> None:
        source = (
            ROOT / "docker/postgis/init/10-roles.sh"
        ).read_text(encoding="utf-8")
        normalized = self.normalized("docker/postgis/init/10-roles.sh")

        self.assertIn(
            'REVOKE CONNECT, TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;',
            normalized,
        )
        self.assertIn(
            'GRANT CONNECT, TEMPORARY ON DATABASE :"DBNAME" '
            'TO :"etl_db_user";',
            normalized,
        )
        self.assertIn(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            source,
        )
        self.assertNotIn("ON SEQUENCES", source)
        self.assert_resource_role_defaults("docker/postgis/init/10-roles.sh")
        self.assert_federation_role_defaults("docker/postgis/init/10-roles.sh")
        self.assertIn(
            'CREATE SCHEMA federation AUTHORIZATION :"federation_db_user";',
            normalized,
        )
        self.assertIn(
            'GRANT CREATE ON DATABASE :"DBNAME" TO :"federation_db_user";',
            normalized,
        )
        self.assertNotIn(
            'GRANT CREATE ON DATABASE :"DBNAME" TO :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            'REVOKE CREATE ON DATABASE :"DBNAME" '
            'FROM :"xyz_db_user", :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            'GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw '
            'TO :"federation_db_user";',
            normalized,
        )
        self.assertIn(
            'REVOKE USAGE ON FOREIGN DATA WRAPPER postgres_fdw '
            'FROM :"xyz_db_user", :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            'REVOKE ALL ON SCHEMA federation '
            'FROM :"xyz_db_user", :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            'LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
            'NOBYPASSRLS PASSWORD',
            normalized,
        )

    def test_upgrade_repairs_legacy_database_and_sequence_privileges(self) -> None:
        source = (
            ROOT / "docker/postgis/upgrade-derived.sh"
        ).read_text(encoding="utf-8")
        normalized = self.normalized("docker/postgis/upgrade-derived.sh")

        self.assertIn(
            'REVOKE CONNECT, TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;',
            normalized,
        )
        self.assertIn(
            'REVOKE TEMPORARY ON DATABASE :"DBNAME" '
            'FROM :"xyz_db_user", :"derived_db_user", '
            ':"federation_db_user";',
            normalized,
        )
        self.assertIn(
            'GRANT CONNECT, TEMPORARY ON DATABASE :"DBNAME" '
            'TO :"etl_db_user";',
            normalized,
        )
        self.assertIn(
            'GRANT CONNECT ON DATABASE :"DBNAME" '
            'TO :"xyz_db_user", :"derived_db_user", '
            ':"federation_db_user";',
            normalized,
        )
        self.assertIn(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            source,
        )
        self.assertIn("BEGIN;", source)
        self.assertIn("COMMIT;", source)
        self.assert_resource_role_defaults("docker/postgis/upgrade-derived.sh")
        self.assert_federation_role_defaults(
            "docker/postgis/upgrade-derived.sh"
        )
        self.assertIn(
            "Refusing to take ownership of existing federation schema",
            source,
        )
        for object_kind in ("object", "schema", "table", "server"):
            with self.subTest(object_kind=object_kind):
                self.assertIn(
                    f"Refusing to migrate known federation {object_kind}",
                    source,
                )
        self.assertIn(
            "owner_role.rolname NOT IN (derived_role, federation_role)",
            normalized,
        )
        self.assertNotIn(
            'ALTER SCHEMA federation OWNER TO :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            'REVOKE CREATE ON DATABASE :"DBNAME" '
            'FROM :"xyz_db_user", :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            'REVOKE USAGE ON FOREIGN DATA WRAPPER postgres_fdw '
            'FROM :"xyz_db_user", :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            "REVOKE USAGE ON FOREIGN SERVER %I FROM %I, %I", normalized
        )
        self.assertIn("alias_record.status = 'active'", normalized)
        self.assertIn(
            "REVOKE USAGE ON SCHEMA %I FROM %I, %I", normalized
        )
        self.assertIn(
            "REVOKE SELECT ON TABLE %I.%I FROM %I, %I", normalized
        )
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA federation",
            normalized,
        )
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA federation",
            normalized,
        )
        self.assert_h3_sql_wrapper_hardening(
            "docker/postgis/upgrade-derived.sh"
        )
        self.assertIn(
            "sh /usr/local/bin/mapp-prepare-spatial-indexes ensure",
            source,
        )

    def test_upgrade_demotes_unbaselined_active_federation_aliases(self) -> None:
        normalized = self.normalized("docker/postgis/upgrade-derived.sh")
        columns = {
            "accepted_schema_fingerprint": "text",
            "accepted_physical_identity": "text",
            "accepted_connection_identity": "text",
            "last_observation_id": "bigint",
        }
        for column, column_type in columns.items():
            with self.subTest(column=column):
                self.assertIn(
                    f"ADD COLUMN IF NOT EXISTS {column} {column_type}",
                    normalized,
                )
                self.assertIn(f"{column} IS NULL", normalized)

        demotion = (
            "UPDATE federation._aliases SET status = 'unavailable' "
            "WHERE provisioned_at IS NOT NULL AND status = 'active'"
        )
        self.assertIn(demotion, normalized)
        self.assertLess(
            normalized.index(demotion), normalized.index("FOR alias_record IN")
        )

    def test_upgrade_refuses_an_unsafe_existing_federation_role_early(
        self,
    ) -> None:
        source = (
            ROOT / "docker/postgis/upgrade-derived.sh"
        ).read_text(encoding="utf-8")
        normalized = self.normalized("docker/postgis/upgrade-derived.sh")
        guard = source.index(
            "Refusing to alter existing federation role % with unsafe attributes"
        )

        self.assertLess(guard, source.index("CREATE EXTENSION IF NOT EXISTS h3;"))
        self.assertLess(
            guard,
            source.index(
                'ALTER ROLE :"federation_db_user" LOGIN PASSWORD'
            ),
        )
        for attribute in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        ):
            with self.subTest(attribute=attribute):
                self.assertIn(f"federation_role.{attribute}", normalized)
        self.assertIn("FROM pg_catalog.pg_auth_members AS membership", normalized)
        self.assertIn(
            "membership.roleid = federation_role.oid OR "
            "membership.member = federation_role.oid",
            normalized,
        )
        self.assertIn(
            "Refusing to alter existing federation role % with memberships",
            source,
        )
        self.assertIn(
            "CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS PASSWORD %L",
            normalized,
        )

    def test_federation_role_collision_fails_before_database_mutation(self) -> None:
        for relative in (
            "docker/postgis/init/10-roles.sh",
            "docker/postgis/upgrade-derived.sh",
        ):
            with self.subTest(relative=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                guard = source.index("FEDERATION_DB_USER must be distinct")
                self.assertLess(guard, source.index("psql "))
                for role in (
                    "POSTGRES_USER",
                    "ETL_DB_USER",
                    "XYZ_DB_USER",
                    "DERIVED_DB_USER",
                ):
                    self.assertIn(
                        f'FEDERATION_DB_USER}}" = "${{{role}', source
                    )

    def test_healthcheck_allows_the_pre_role_split_upgrade_to_start(self) -> None:
        healthcheck = (
            ROOT / "docker/postgis/healthcheck.sh"
        ).read_text(encoding="utf-8")
        wrapper = (ROOT / "bin/mapp").read_text(encoding="utf-8")

        self.assertIn('up --detach --build --no-deps --wait db', wrapper)
        self.assertIn(
            'exec -T db sh /usr/local/bin/mapp-upgrade-derived', wrapper
        )
        self.assertNotIn("federation_db_user", healthcheck)
        self.assertNotIn("nspname = 'federation'", healthcheck)

    def test_verifier_covers_reader_derived_and_census_audit_edges(self) -> None:
        source = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")

        required_contracts = (
            "DERIVED_DATABASE_URL value resolved from the current environment",
            "DERIVED_READER_ROLE value resolved from the current environment",
            "defaults.defaclobjtype = 'r'",
            "Bundled database CONNECT and TEMPORARY must be revoked from PUBLIC",
            "Runtime reader and derived owner defaults must not permit sequence mutation",
            'AS "canUseFdw"',
            'AS "canCreateSchema"',
            'AS "canCreateDatabaseObject"',
            'audit["canUseFdw"]',
            'audit["canCreateSchema"]',
            "audit.canUseFdw",
            "audit.canCreateDatabaseObject",
            'current_user::text AS "currentUser"',
            'session_user::text AS "sessionUser"',
            'AS "hasTemporary"',
            'AS "hasPublicDatabasePrivilege"',
            'AS "hasUnsafeMembership"',
            "pg_has_role(",
            "reachable_role.rolbypassrls",
            "audit.currentUser !== uriUser",
            "audit.currentUser !== audit.sessionUser",
            "configuration service DBS_MAPP session",
            "for service in xyz xyz-preview config-ui",
            "is running with unresolved placeholders in DBS_MAPP",
            "Run ./bin/mapp up --force-recreate to replace the stale containers",
            "reader_resource_limits",
            "derived_resource_limits",
            'current_setting($$search_path$$) AS "searchPath"',
            'audit["searchPath"] != "pg_catalog, public"',
            "owner search_path must be ",
            "exactly pg_catalog, public.",
            "tempFileLimitKb",
            "transactionTimeoutMs",
            "temporary-file, parallelism, and timeout limits",
            'audit["databaseName"] != reader_session["database_name"]',
            "active DERIVED_DATABASE_URL session",
            "oa21cd IS NULL OR oa21cd !~ '^E[0-9]{8}$'",
            "count(DISTINCT oa21cd)::bigint",
            "run.geometry_repairs = dataset.geometry_repairs",
            "source_metadata #>> '{geometry,repairs}'",
            "geometry_source_sha256 = expected_geometry_sha256",
            "source_metadata #>> '{geometry,sha256}'",
            "jsonb_each_text(expected_topic_hashes)",
            "variable_topic_hash_mismatch_count",
            "dataset_topic_hash_mismatch_count",
            "MAPP_VERIFY_CENSUS_TOPIC_HASHES_JSON",
            "metadataTopicHashes.get(topicId) !== expectedHash",
            "variableTopicHashes.get(topicId) !== expectedHash",
            "mapp-prepare-spatial-indexes check",
        )
        for contract in required_contracts:
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

    def test_derived_and_federation_ownership_are_separate(self) -> None:
        normalized = self.normalized("scripts/verify.sh")
        self.assertNotIn(
            "namespace.nspname IN ($$derived_layers$$, $$federation$$)",
            normalized,
        )
        self.assertNotIn("FROM federation._aliases AS fed_alias", normalized)
        self.assertIn(
            "namespace.nspowner = login_role.oid AND "
            "namespace.nspname = $$derived_layers$$",
            normalized,
        )
        self.assertIn(
            'federation_audit["ownsFederationSchema"]', normalized
        )
        self.assertIn('federation_audit["canCreateSchema"]', normalized)
        self.assertIn(
            'federation_audit["hasDerivedSchemaPrivilege"]', normalized
        )
        self.assertIn(
            'federation_audit["hasDerivedObjectPrivilege"]', normalized
        )
        self.assertIn('AS "hasRegistrySchemaAccess"', normalized)
        self.assertIn('AS "hasRegistryObjectAccess"', normalized)
        self.assertIn("has_any_column_privilege(", normalized)
        self.assertIn("$$USAGE,CREATE$$", normalized)
        self.assertIn(
            "must not access the", normalized
        )
        self.assertIn("federation control registry.", normalized)

    def test_verifier_checks_the_foreign_server_matches_its_connection_ref(
        self,
    ) -> None:
        # Resolving connectionRef needs os.environ, so the verifier compares
        # each provisioned server against its registered connection directly.
        normalized = self.normalized("scripts/verify.sh")
        self.assertIn(
            '"SELECT alias, connection_ref, allowed_relations, status, "',
            normalized,
        )
        self.assertIn(
            '"last_observation, accepted_schema_fingerprint, "', normalized
        )
        self.assertIn('"WHERE provisioned_at IS NOT NULL"', normalized)
        self.assertIn(
            'f"FEDERATION_DBS_{connection_ref}"', normalized
        )
        self.assertIn("srvoptions", normalized)
        self.assertIn(
            "FROM pg_catalog.pg_foreign_server WHERE srvname = %s",
            normalized,
        )
        self.assertIn(
            "has_server_privilege(%s, oid, $$USAGE$$)", normalized
        )
        self.assertIn('server["derived_use"]', normalized)
        self.assertIn('server["reader_use"]', normalized)
        for field in (
            "host", "hostaddr", "port", "dbname", "sslmode",
            "sslrootcert", "gssencmode",
        ):
            with self.subTest(field=field):
                self.assertIn(f'"{field}"', normalized)
        self.assertIn("actual_options != expected_options", normalized)
        self.assertIn("unexpected_options = set(server_options)", normalized)
        self.assertIn("pushdown_safe = all(", normalized)
        self.assertIn('alias_row["status"] == "active"', normalized)
        self.assertIn("and not pushdown_safe", normalized)
        self.assertIn("remote_relation != expected_relations", normalized)
        self.assertIn(
            'relation["derived_select"] != expected_usage', normalized
        )
        self.assertIn(
            'relation["reader_select"] != expected_usage', normalized
        )
        self.assertIn("FROM pg_catalog.pg_user_mappings", normalized)
        self.assertIn(
            "required_mapping_roles.issubset(mappings)", normalized
        )
        self.assertIn(
            "set(mappings).issubset(allowed_mapping_roles)", normalized
        )
        self.assertIn(
            'alias_row["status"] == "active" and set(mappings) != '
            "allowed_mapping_roles",
            normalized,
        )
        self.assertIn(
            "mappings[federation_role] != expected_mapping", normalized
        )

    def test_verifier_rejects_unsafe_federation_acl_edges(self) -> None:
        normalized = self.normalized("scripts/verify.sh")
        for contract in (
            "nspacl, pg_catalog.acldefault($$n$$, nspowner)",
            "privilege.grantee = nspowner",
            "privilege.grantor = nspowner",
            "privilege.privilege_type = $$USAGE$$",
            "relation.relacl, pg_catalog.acldefault( "
            "$$r$$, relation.relowner )",
            "privilege.grantee = relation.relowner",
            "privilege.grantor = relation.relowner",
            "privilege.privilege_type = $$SELECT$$",
            "NOT privilege.is_grantable",
            "attribute.attacl IS NOT NULL",
            'schema["hasUnexpectedAcl"]',
            'relation["hasUnexpectedAcl"]',
            'relation["hasColumnAcl"]',
            "FROM pg_catalog.pg_auth_members AS membership",
            "consumer_role.oid = membership.roleid",
            "consumer_role.rolname IN (%s, %s)",
            'cursor.fetchone()["hasConsumerRoleMember"]',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, normalized)
        self.assertEqual(2, normalized.count("pg_catalog.aclexplode(COALESCE("))
        self.assertEqual(2, normalized.count("%s AND privilege.grantor"))
        self.assertEqual(2, normalized.count("AND NOT privilege.is_grantable)"))

    def test_verifier_rejects_active_aliases_without_accepted_evidence(
        self,
    ) -> None:
        normalized = self.normalized("scripts/verify.sh")
        for field in (
            "accepted_schema_fingerprint",
            "accepted_physical_identity",
            "accepted_connection_identity",
            "last_observation_id",
        ):
            with self.subTest(field=field):
                self.assertIn(f'"{field}"', normalized)
        self.assertIn('alias_row["status"] == "active"', normalized)
        self.assertIn("alias_row[field] is None", normalized)
        self.assertIn("without complete accepted evidence", normalized)

    def test_bundled_spatial_index_preparer_covers_managed_relations(self) -> None:
        source = self.normalized(
            "docker/postgis/prepare-spatial-indexes.sh"
        )
        for contract in (
            "prepare|ensure|check",
            "type.typname IN ('geometry', 'geography')",
            "access_method.amname = 'gist'",
            "public.ST_Transform(%I, 4326)",
            "public.ST_Transform(%I, 3857)",
            "public.ST_Transform(%I, 27700)",
            "::public.geometry",
            "::public.geography",
            "ANALYZE %I.%I",
            "exists but is not a valid ready non-partial GiST index",
            "has no valid native GiST index; run ./bin/mapp upgrade-derived",
            "is missing its valid ready % GiST index; run ./bin/mapp upgrade-derived",
            "ensure_only",
            "NOT ensure_only OR index_created",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

        compose = (
            ROOT / "compose.bundled-db.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "./docker/postgis/init/25-platform-layer-drop-guard.sql:/docker-entrypoint-initdb.d/85-mapp-platform-layer-drop-guard.sql:ro",
            compose,
        )
        wrapper = (ROOT / "bin/mapp").read_text(encoding="utf-8")
        self.assertIn("mapp-prepare-spatial-indexes:ro", compose)
        self.assertIn(
            "prepare_spatial_indexes()",
            wrapper,
        )
        self.assertIn(
            'exec -T db sh /usr/local/bin/mapp-prepare-spatial-indexes',
            wrapper,
        )
        self.assertIn("ensure_bundled_database_upgraded()", wrapper)
        # One definition and four call sites: up, serve --force-recreate,
        # config-ui and all. It was six until the etl and census-etl
        # subcommands were deleted -- nothing loads into this database now.
        self.assertGreaterEqual(
            wrapper.count("ensure_bundled_database_upgraded"),
            5,
        )
        self.assertIn(
            'exec -T db sh /usr/local/bin/mapp-upgrade-derived',
            wrapper,
        )
        self.assertIn(
            'up --detach --no-deps --wait db',
            wrapper,
        )

    def test_verifier_reads_the_closed_census_manifest_without_jq(self) -> None:
        source = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")

        for contract in (
            '"${ROOT_DIR}/instance/etl/census.json"',
            "from leeds_arcgis_etl.census_config import load_census_config",
            "config = load_census_config(sys.argv[1])",
            "config.geometry_sha256",
            "topic_hashes = {topic.id: topic.sha256",
            'json.dumps(topic_hashes, sort_keys=True, separators=(",", ":"))',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertNotIn("jq ", source)

    def test_verifier_checks_platform_layer_drop_guard_objects(self) -> None:
        source = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
        for contract in (
            "Layer dependency guard table public.mapp_platform_layer_dependencies is missing.",
            "Layer dependency sync function public.mapp_sync_platform_layer_dependencies is missing.",
            "Layer dependency sync function public.mapp_sync_platform_layer_dependencies(text, jsonb) is missing.",
            "PUBLIC does not have execute permission on public.mapp_sync_platform_layer_dependencies(text, jsonb).",
            "Layer drop guard event trigger mapp_block_platform_layer_drops is missing.",
            "Layer drop guard function public.mapp_block_platform_layer_drops is missing.",
            "to_regprocedure('public.mapp_sync_platform_layer_dependencies(text, jsonb)')",
            "pg_event_trigger",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

    def test_inline_derived_session_probe_is_valid_python(self) -> None:
        source = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
        marker = '"${compose[@]}" exec -T config-ui python -c \'\n'
        start = source.rindex(marker) + len(marker)
        end = source.index(
            "\n' \"$(dotenv_value DERIVED_DB_USER)\"",
            start,
        )

        compile(source[start:end], "verify-derived-session", "exec")

    def test_the_database_mode_axis_is_gone(self) -> None:
        """No file may reason about which database a deployment runs.

        The MAPP database is packaged in every deployment, so there is nothing
        left to branch on. This asserts absence over a named file list rather
        than matching a syntax, because the syntax it would have matched no
        longer exists anywhere -- a pattern-based check would pass by finding
        nothing rather than by the property holding.

        It is the only mechanism in either repository that can catch a missed
        has_bundled_database call site. An undefined function inside an "if !"
        condition is not fatal under set -euo pipefail: bash returns 127, the
        negation inverts it, and the caller proceeds as though the check had
        passed. bin/mapp:455 guarded the database upgrade that way, so a missed
        site there would make every serve and up skip the upgrade and exit 0.

        The list grows as each remaining file is cleared.
        """
        offenders = []
        for relative_path in (
            "bin/mapp",
            "scripts/verify.sh",
            "scripts/production_acceptance.py",
            "compose.yaml",
            ".env.example",
            "scripts/federation-e2e.sh",
        ):
            lines = (ROOT / relative_path).read_text(
                encoding="utf-8"
            ).splitlines()
            for index, line in enumerate(lines):
                if "MAPP_DATABASE_MODE" in line or "has_bundled_database" in line:
                    offenders.append(
                        f"{relative_path}:{index + 1}:{line.strip()}"
                    )

        self.assertEqual(offenders, [])

    def test_external_handoff_revokes_public_database_defaults(self) -> None:
        source = (
            ROOT / "docs/external-postgresql.md"
        ).read_text(encoding="utf-8")

        for contract in (
            "Use a dedicated database for MAPP",
            "REVOKE CONNECT, TEMPORARY ON DATABASE maps FROM PUBLIC;",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            "ALTER ROLE mapp_derived_owner SET search_path = pg_catalog, public;",
            "ALTER FUNCTION public.h3_polygon_to_cells(public.geometry, integer)",
            "routine namespace must equal `pg_extension.extnamespace`",
            "`current_user`,\n`session_user`, and the decoded URI username",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)


if __name__ == "__main__":
    unittest.main()
