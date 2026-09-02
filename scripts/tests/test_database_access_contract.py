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
            'ALTER ROLE :"xyz_db_user" CONNECTION LIMIT 50;',
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

    def test_live_test_tokens_have_failure_safe_revocation_sweeps(self) -> None:
        """Container tests must not leave usable bearer credentials behind."""
        contracts = {
            "docker/demo-sources/layers.sh": (
                '"demo-layers-" + secrets.token_hex(8)',
                'name.startswith("demo-layers-")',
                'then revoke_token "${TOKEN_ID}"; fi; revoke_demo_tokens\' EXIT',
            ),
            "scripts/federation-e2e.sh": (
                'name.startswith("federation-e2e")',
                "store.revoke_token(record[\"id\"])",
                "trap cleanup EXIT",
            ),
        }
        for relative_path, required in contracts.items():
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            for contract in required:
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

    def test_the_semantic_store_stays_under_its_role_connection_limit(self) -> None:
        """The store's own gate must sit below the DDL's CONNECTION LIMIT.

        These two numbers live in different repositories-worth of file and are
        only correct together. The service is a ThreadingHTTPServer opening one
        connection per request with no pool, so if the gate is ever raised
        above the role limit -- or the role limit lowered beneath it -- the
        excess requests are refused by PostgreSQL with "too many connections
        for role" and surface as HTTP 500s. Eight parallel healthchecks were
        enough to reproduce that before the gate existed.

        The semantic-service suite cannot catch this: its scratch database
        connects as an unrestricted role, so the limit is not in play there.
        """
        roles = (ROOT / "docker/postgis/init/10-roles.sh").read_text(
            encoding="utf-8"
        )
        store = (ROOT / "semantic-service/semantic_store.py").read_text(
            encoding="utf-8"
        )

        limits = {
            int(value)
            for value in re.findall(
                r'ALTER ROLE :"semantic(?:_reader)?_db_user" '
                r"CONNECTION LIMIT (\d+);",
                roles,
            )
        }
        self.assertEqual(
            2, len(re.findall(r'semantic(?:_reader)?_db_user" CONNECTION LIMIT', roles)),
            "expected a connection limit on both semantic roles",
        )
        gate = re.search(r"^MAX_CONCURRENT_CONNECTIONS = (\d+)$", store, re.M)
        self.assertIsNotNone(gate, "the store declares no connection gate")

        self.assertTrue(limits, "no semantic CONNECTION LIMIT found")
        self.assertLess(
            int(gate.group(1)),
            min(limits),
            "the store may open more connections than its role allows",
        )

    def test_packaged_connection_budgets_cover_all_runtime_consumers(self) -> None:
        """Role gates must admit both XYZ pools and every source consumer."""
        def service_body(source: str, service: str) -> str:
            service_block = re.search(
                rf"^  {re.escape(service)}:\n(?P<body>.*?)"
                r"(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                source,
                re.M | re.S,
            )
            self.assertIsNotNone(service_block, f"missing {service} service")
            return service_block.group("body")

        def postgres_setting(body: str, setting: str) -> int:
            values = re.findall(
                rf"(?<![A-Za-z0-9_]){re.escape(setting)}=(\d+)",
                body,
            )
            self.assertEqual(
                1,
                len(values),
                f"expected one explicit {setting} setting",
            )
            return int(values[0])

        dockerfile = (ROOT / "docker/xyz/Dockerfile").read_text(
            encoding="utf-8"
        )
        pool_match = re.search(
            r"^ARG XYZ_DB_POOL_CONNECTIONS=(\d+)$", dockerfile, re.M
        )
        self.assertIsNotNone(pool_match, "the pinned XYZ pool size is not declared")
        self.assertIn(
            'grep -F "max: ${XYZ_DB_POOL_CONNECTIONS}," mod/utils/dbs.js',
            dockerfile,
            "the image build does not verify its upstream pool-size assumption",
        )
        pool_size = int(pool_match.group(1))

        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        pool_services = ("xyz", "xyz-preview")
        for service in pool_services:
            self.assertIn("DBS_MAPP:", service_body(compose, service))
        supervisor = (ROOT / "docker/xyz/supervisor.mjs").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            1,
            supervisor.count('spawn("node", ["express.js"]'),
            "one XYZ service can start more than one upstream pool process",
        )

        roles = (ROOT / "docker/postgis/init/10-roles.sh").read_text(
            encoding="utf-8"
        )
        upgrade = (ROOT / "docker/postgis/upgrade-derived.sh").read_text(
            encoding="utf-8"
        )

        def role_limit(source: str, variable: str) -> int:
            match = re.search(
                rf'ALTER ROLE :"{variable}" CONNECTION LIMIT (\d+);',
                source,
            )
            self.assertIsNotNone(match, f"missing limit for {variable}")
            return int(match.group(1))

        runtime_limit = role_limit(roles, "xyz_db_user")
        runtime_database = (ROOT / "config-ui/runtime_database.py").read_text(
            encoding="utf-8"
        )
        admission_match = re.search(
            r"^MAX_CONCURRENT_DBS_CONNECTIONS = (\d+)$",
            runtime_database,
            re.M,
        )
        self.assertIsNotNone(
            admission_match,
            "the configuration service declares no shared DBS_* admission bound",
        )
        admitted_configuration_connections = int(admission_match.group(1))
        self.assertEqual(8, admitted_configuration_connections)
        for relative_path, expected_uses in (
            ("config-ui/app.py", 6),
            ("config-ui/control_api.py", 2),
            ("config-ui/semantic_sources.py", 3),
        ):
            caller = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("from runtime_database import dbs_connection", caller)
            self.assertEqual(
                expected_uses,
                caller.count("with dbs_connection("),
                f"{relative_path} has an unreviewed DBS_* connection site",
            )
        semantic_sources = (ROOT / "config-ui/semantic_sources.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("connection_context=dbs_connection", semantic_sources)
        config_image = (ROOT / "config-ui/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("runtime_database.py", config_image)
        runtime_probe_headroom = 2
        self.assertEqual(
            len(pool_services) * pool_size
            + admitted_configuration_connections
            + runtime_probe_headroom,
            runtime_limit,
            "the runtime role cannot admit both XYZ pools, the bounded "
            "configuration service, and probe headroom",
        )
        self.assertEqual(
            runtime_limit,
            role_limit(upgrade, "xyz_db_user"),
            "the existing-volume upgrade has a different runtime budget",
        )

        bundled_compose = (ROOT / "compose.bundled-db.yaml").read_text(
            encoding="utf-8"
        )
        demo_compose = (ROOT / "compose.federated-demo.yaml").read_text(
            encoding="utf-8"
        )
        federation_compose = (ROOT / "compose.federation-test.yaml").read_text(
            encoding="utf-8"
        )
        cluster_services = (
            (bundled_compose, "db"),
            (demo_compose, "census-db"),
            (demo_compose, "ops-db"),
            (federation_compose, "source-db"),
        )
        expected_cluster_settings = {
            "max_connections": 100,
            "superuser_reserved_connections": 3,
            "reserved_connections": 0,
        }
        for cluster_compose, service in cluster_services:
            body = service_body(cluster_compose, service)
            with self.subTest(service=service):
                for setting, expected in expected_cluster_settings.items():
                    self.assertEqual(
                        expected,
                        postgres_setting(body, setting),
                    )
        ordinary_cluster_capacity = (
            expected_cluster_settings["max_connections"]
            - expected_cluster_settings["superuser_reserved_connections"]
            - expected_cluster_settings["reserved_connections"]
        )
        self.assertEqual(97, ordinary_cluster_capacity)
        bundled_role_limit_total = sum(
            role_limit(roles, variable)
            for variable in (
                "etl_db_user",
                "xyz_db_user",
                "derived_db_user",
                "federation_db_user",
                "semantic_db_user",
                "semantic_reader_db_user",
            )
        )
        self.assertLessEqual(
            bundled_role_limit_total,
            ordinary_cluster_capacity,
            "the packaged role maxima exceed ordinary PostgreSQL capacity",
        )

        source_roles = (ROOT / "docker/source-db/init/01-roles.sh").read_text(
            encoding="utf-8"
        )
        demo_seed = (ROOT / "docker/demo-sources/seed.sh").read_text(
            encoding="utf-8"
        )
        federation_seed = (ROOT / "docker/source-db/seed.sh").read_text(
            encoding="utf-8"
        )
        federation_e2e = (ROOT / "scripts/federation-e2e.sh").read_text(
            encoding="utf-8"
        )
        source_limit = role_limit(source_roles, "reader_user")
        semantic_limit_match = re.search(
            r"^MAX_CONCURRENT_GENERATION_CONTEXT_READS = (\d+)$",
            semantic_sources,
            re.M,
        )
        self.assertIsNotNone(
            semantic_limit_match,
            "optional semantic context declares no server-side admission bound",
        )
        source_spare = 3
        expected_source_limit = (
            runtime_limit
            + role_limit(roles, "derived_db_user")
            + role_limit(roles, "federation_db_user")
            + int(semantic_limit_match.group(1))
            + source_spare
        )
        self.assertEqual(
            expected_source_limit,
            source_limit,
            "the source reader cannot admit every bounded platform consumer",
        )
        self.assertLessEqual(
            source_limit,
            ordinary_cluster_capacity,
            "the packaged source-reader maximum exceeds ordinary PostgreSQL capacity",
        )
        self.assertEqual(
            source_limit,
            role_limit(demo_seed, "reader_user"),
            "mapp demo does not upgrade retained source volumes",
        )
        self.assertEqual(
            source_limit,
            role_limit(federation_seed, "reader_user"),
            "the standalone federation seed does not upgrade its retained source",
        )
        self.assertEqual(
            source_limit,
            role_limit(federation_e2e, "reader_user"),
            "the federation harness does not upgrade its retained source",
        )
        verifier = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
        self.assertIn(
            f'"connectionLimit": ({runtime_limit}, {runtime_limit})',
            verifier,
            "verification accepts a stale or over-broad runtime role limit",
        )
        for capacity_contract in (
            'expected_cluster_settings = (100, 3, 0)',
            'settings.max_connections AS "maxConnections"',
            'AS "superuserReservedConnections"',
            'settings.reserved_connections AS "reservedConnections"',
            "active_cluster_settings != expected_cluster_settings",
        ):
            self.assertIn(
                capacity_contract,
                verifier,
                "verification does not audit the active PostgreSQL capacity",
            )
        external_docs = (ROOT / "docs/external-postgresql.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            f"ALTER ROLE mapp_runtime_reader CONNECTION LIMIT {runtime_limit};",
            external_docs,
        )
        api_contract = (ROOT / "docs/api-contract.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"{source_limit}-session budget", api_contract)

    def test_semantic_context_reads_stay_under_the_source_role_limit(self) -> None:
        """Optional data context must leave source-reader headroom.

        The dashboard can request many field drafts at once, but every sample
        or statistics request opens a source connection before Gemini runs.
        Keep the configuration service's process-wide admission bound tied to
        the packaged source role instead of relying on client throttling.
        """
        roles = (ROOT / "docker/source-db/init/01-roles.sh").read_text(
            encoding="utf-8"
        )
        sources = (ROOT / "config-ui/semantic_sources.py").read_text(
            encoding="utf-8"
        )

        role_limit = re.search(
            r'ALTER ROLE :"reader_user" CONNECTION LIMIT (\d+);',
            roles,
        )
        self.assertIsNotNone(
            role_limit,
            "the packaged source reader declares no connection limit",
        )
        admission_limit = re.search(
            r"^MAX_CONCURRENT_GENERATION_CONTEXT_READS = (\d+)$",
            sources,
            re.M,
        )
        self.assertIsNotNone(
            admission_limit,
            "optional semantic context declares no server-side admission bound",
        )
        self.assertLess(
            int(admission_limit.group(1)),
            int(role_limit.group(1)),
            "optional semantic context may exhaust the source-reader role",
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
            'FROM :"xyz_db_user", :"derived_db_user", '
            ':"semantic_db_user", :"semantic_reader_db_user";',
            normalized,
        )
        self.assertIn(
            'GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw '
            'TO :"federation_db_user";',
            normalized,
        )
        self.assertIn(
            'REVOKE USAGE ON FOREIGN DATA WRAPPER postgres_fdw '
            'FROM :"xyz_db_user", :"derived_db_user", '
            ':"semantic_db_user", :"semantic_reader_db_user";',
            normalized,
        )
        self.assertIn(
            'REVOKE ALL ON SCHEMA federation '
            'FROM :"xyz_db_user", :"derived_db_user", '
            ':"semantic_db_user", :"semantic_reader_db_user";',
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
