from __future__ import annotations

import re
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
            "ALTER FUNCTION %s SET search_path = pg_catalog, public",
            "IF hardened_count <> 4 THEN",
        ):
            with self.subTest(path=relative_path, contract=contract):
                self.assertIn(contract, source)

    def test_fresh_h3_install_hardens_catalog_owned_sql_wrappers(self) -> None:
        self.assert_h3_sql_wrapper_hardening(
            "docker/postgis/init/05-h3.sql"
        )

    def test_wrappers_reject_every_database_environment_override(self) -> None:
        expected = {
            "DBS_MAPP",
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
            "DERIVED_READER_ROLE",
        }
        for relative_path in ("bin/mapp", "scripts/verify.sh"):
            with self.subTest(path=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                start = source.index("reject_database_environment_overrides()")
                end = source.index("\n}", start)
                guard = source[start:end]
                for key in expected:
                    self.assertIn(key, guard)

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
        self.assertIn(
            'GRANT SELECT ON TABLES TO :"xyz_db_user";',
            source,
        )
        self.assertIn(
            'GRANT SELECT ON TABLES TO :"derived_db_user";',
            source,
        )
        self.assertNotIn("ON SEQUENCES", source)
        self.assert_resource_role_defaults("docker/postgis/init/10-roles.sh")

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
            'FROM :"xyz_db_user", :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            'GRANT CONNECT, TEMPORARY ON DATABASE :"DBNAME" '
            'TO :"etl_db_user";',
            normalized,
        )
        self.assertIn(
            'GRANT CONNECT ON DATABASE :"DBNAME" '
            'TO :"xyz_db_user", :"derived_db_user";',
            normalized,
        )
        self.assertIn(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            source,
        )
        self.assertIn(
            'GRANT SELECT ON ALL TABLES IN SCHEMA leeds '
            'TO :"xyz_db_user";',
            normalized,
        )
        self.assertIn(
            'GRANT SELECT ON TABLES TO :"xyz_db_user";',
            source,
        )
        self.assertIn("BEGIN;", source)
        self.assertIn("COMMIT;", source)
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA leeds",
            source,
        )
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON SEQUENCES",
            source,
        )
        self.assert_resource_role_defaults("docker/postgis/upgrade-derived.sh")
        self.assert_h3_sql_wrapper_hardening(
            "docker/postgis/upgrade-derived.sh"
        )

    def test_verifier_covers_reader_derived_and_census_audit_edges(self) -> None:
        source = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")

        required_contracts = (
            "DERIVED_DATABASE_URL value resolved from the current environment",
            "DERIVED_READER_ROLE value resolved from the current environment",
            "defaults.defaclobjtype = 'r'",
            "Bundled database CONNECT and TEMPORARY must be revoked from PUBLIC",
            "Runtime reader and derived owner defaults must not permit sequence mutation",
            "Runtime reader and derived owner must both read %",
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

    def test_bundled_spatial_index_preparer_covers_managed_relations(self) -> None:
        source = self.normalized(
            "docker/postgis/prepare-spatial-indexes.sh"
        )
        for contract in (
            "namespace.nspname IN ('leeds', 'derived_layers')",
            "type.typname IN ('geometry', 'geography')",
            "access_method.amname = 'gist'",
            "public.ST_Transform(%I, 4326)",
            "public.ST_Transform(%I, 3857)",
            "::public.geometry",
            "::public.geography",
            "ANALYZE %I.%I",
            "exists but is not a valid ready non-partial GiST index",
            "has no valid native GiST index; run ./bin/mapp upgrade-derived",
            "is missing its valid ready % GiST index; run ./bin/mapp upgrade-derived",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

        compose = (
            ROOT / "compose.bundled-db.yaml"
        ).read_text(encoding="utf-8")
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

    def test_inline_derived_session_probe_is_valid_python(self) -> None:
        source = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
        marker = '"${compose[@]}" exec -T config-ui python -c \'\n'
        start = source.rindex(marker) + len(marker)
        end = source.index(
            "\n' \"${database_mode}\" \"$(dotenv_value DERIVED_DB_USER)\"",
            start,
        )

        compile(source[start:end], "verify-derived-session", "exec")

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
