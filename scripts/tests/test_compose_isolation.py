from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]


def resolved_compose(*overlays: str) -> dict[str, Any]:
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(ROOT / ".env.example"),
        "--file",
        str(ROOT / "compose.yaml"),
        "--profile",
        "tools",
    ]
    for overlay in overlays:
        command.extend(("--file", str(ROOT / overlay)))
    command.extend(("config", "--format", "json"))
    environment = os.environ.copy()
    environment.update({
        "PRODUCTION_MAP_SITE": "https://maps.company.co.uk",
        "PRODUCTION_CONFIG_SITE": "https://config.company.co.uk",
        "PRODUCTION_CONFIG_ALLOWED_HOSTS": "config.company.co.uk,config-ui",
        "PRODUCTION_MCP_SITE": "https://mcp.company.co.uk",
        "PRODUCTION_CADDY_EMAIL": "operations@company.co.uk",
    })
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )
    return json.loads(completed.stdout)


class ComposeIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Compose model resolution is static and does not contact the daemon or
        # start a stack.
        cls.models = {
            "external": resolved_compose(),
            "bundled": resolved_compose("compose.bundled-db.yaml"),
            "external-production": resolved_compose("compose.production.yaml"),
            "bundled-production": resolved_compose(
                "compose.bundled-db.yaml",
                "compose.production.yaml",
            ),
        }

    def test_semantic_service_remains_private_and_holds_only_its_own_roles(
        self,
    ) -> None:
        """The catalogue moved from a bind-mounted SQLite file into the database.

        Before the move semantic-service held no database credential and had
        no route to one, so its containment was topological. Now it holds two
        login roles and sits on the backend network, and the bound is entirely
        grants -- which scripts/verify.sh audits. What this test still pins is
        the compose half: it is given the semantic roles and no other, it is
        still unreachable from outside, and its state no longer lives in a
        file on the host.
        """
        template = dict(
            line.split("=", 1)
            for line in (ROOT / ".env.example").read_text(
                encoding="utf-8"
            ).splitlines()
            if "=" in line and not line.startswith("#")
        )
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                service = model["services"]["semantic-service"]
                environment = service.get("environment", {})
                expected = {"PORT", "SEMANTIC_INTERNAL_TOKEN"}
                if "bundled" in mode:
                    expected |= {
                        "SEMANTIC_DATABASE_URL",
                        "SEMANTIC_READER_DATABASE_URL",
                    }
                self.assertEqual(expected, set(environment))

                # The credential it is handed must be its own. A DSN carrying
                # any other role would hand the catalogue service the reach of
                # that role, and no grant audit downstream would notice. The
                # username is parsed rather than searched for: every role name
                # here shares the POSTGRES_USER prefix, so a substring test
                # passes on any of them.
                for key, user in (
                    ("SEMANTIC_DATABASE_URL", "SEMANTIC_DB_USER"),
                    ("SEMANTIC_READER_DATABASE_URL", "SEMANTIC_READER_DB_USER"),
                ):
                    url = environment.get(key)
                    if url is None:
                        continue
                    self.assertEqual(
                        template[user],
                        urlsplit(url).username,
                        key,
                    )

                # No bind mount: the SQLite file it used to hold is gone.
                self.assertFalse(service.get("volumes"))
                self.assertFalse(service.get("ports"))
                self.assertEqual(
                    {"semantic-control", "backend"},
                    set(service.get("networks", {})),
                )
                self.assertTrue(model["networks"]["semantic-control"]["internal"])
                self.assertNotIn(
                    "/var/run/docker.sock",
                    json.dumps(service, sort_keys=True),
                )

    def test_federation_source_reference_is_only_given_to_config_ui(self) -> None:
        model = resolved_compose(
            "compose.bundled-db.yaml",
            "compose.federation-test.yaml",
        )
        services = model["services"]
        config_environment = services["config-ui"]["environment"]

        self.assertIn("FEDERATION_DBS_LEEDS_EXT", config_environment)
        self.assertNotIn("DBS_LEEDS_EXT", config_environment)
        for service_name in ("xyz", "xyz-preview", "semantic-service"):
            with self.subTest(service=service_name):
                self.assertNotIn(
                    "FEDERATION_DBS_LEEDS_EXT",
                    services[service_name].get("environment", {}),
                )

    def test_demo_source_references_are_only_given_to_config_ui(self) -> None:
        """The two-source demo overlay must keep the same boundary as the rig.

        It also proves the overlay resolves from .env.example at all: the
        overlay shipped without its keys in the template, so `docker compose
        config` failed with "required variable FEDERATION_DBS_CENSUS is
        missing" and nothing could reach it.
        """
        model = resolved_compose(
            "compose.bundled-db.yaml",
            "compose.federated-demo.yaml",
        )
        services = model["services"]
        config_environment = services["config-ui"]["environment"]

        for reference in ("FEDERATION_DBS_CENSUS", "FEDERATION_DBS_OPS"):
            with self.subTest(reference=reference):
                self.assertIn(reference, config_environment)
                # A DBS_<REF> would make the source an ordinary workspace
                # connection, bypassing the federation registry entirely.
                self.assertNotIn(reference.replace("FEDERATION_", ""),
                                 config_environment)
                for service_name in ("xyz", "xyz-preview", "semantic-service"):
                    self.assertNotIn(
                        reference,
                        services[service_name].get("environment", {}),
                    )

    def test_both_opt_in_overlays_compose_together(self) -> None:
        """Each overlay forwards only its own references.

        Recreating config-ui with one overlay drops the other's, and verify
        then fails with "connectionRef is not configured". Naming both is the
        supported composition, so it is pinned here.
        """
        services = resolved_compose(
            "compose.bundled-db.yaml",
            "compose.federation-test.yaml",
            "compose.federated-demo.yaml",
        )["services"]
        config_environment = services["config-ui"]["environment"]

        for reference in (
            "FEDERATION_DBS_LEEDS_EXT",
            "FEDERATION_DBS_CENSUS",
            "FEDERATION_DBS_OPS",
        ):
            self.assertIn(reference, config_environment)
        for service_name in ("census-db", "ops-db", "source-db"):
            self.assertIn(service_name, services)

    def test_config_ui_is_the_only_semantic_service_peer(self) -> None:
        semantic_state = str((ROOT / "var/semantic").resolve())
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                services = model["services"]
                peers = {
                    name
                    for name, service in services.items()
                    if "semantic-control" in service.get("networks", {})
                }
                self.assertEqual({"config-ui", "semantic-service"}, peers)

                config = services["config-ui"]
                semantic = services["semantic-service"]
                self.assertEqual(
                    "http://semantic-service:8080",
                    config["environment"]["SEMANTIC_SERVICE_URL"],
                )
                self.assertEqual(
                    semantic["environment"]["SEMANTIC_INTERNAL_TOKEN"],
                    config["environment"]["SEMANTIC_INTERNAL_TOKEN"],
                )
                self.assertEqual(
                    "service_healthy",
                    config["depends_on"]["semantic-service"]["condition"],
                )
                config_volumes = config.get("volumes", [])
                self.assertNotIn(
                    "/state",
                    {volume["target"] for volume in config_volumes},
                )
                self.assertNotIn(
                    semantic_state,
                    {volume.get("source") for volume in config_volumes},
                )

    def test_the_authorization_component_is_off_the_public_network(self) -> None:
        """Its whole public surface is one Unix socket Caddy connects to.

        compose.yaml said so in a comment and then listed `edge` anyway, which
        put the control listener -- the RFC 8693 exchange, introspection and
        revocation -- on the same network as every edge service, reachable by
        DNS name. Nothing constrained membership in either direction: deleting
        the line changed no test, and neither did adding it.
        """
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                services = model["services"]
                mcp_auth = services["mcp-auth"]
                self.assertEqual(
                    {"backend", "mcp-control"}, set(mcp_auth["networks"])
                )
                edge_members = {
                    name
                    for name, service in services.items()
                    if "edge" in service.get("networks", {})
                }
                self.assertNotIn("mcp-auth", edge_members)
                # And nothing else joins the control link.
                control_members = {
                    name
                    for name, service in services.items()
                    if "mcp-control" in service.get("networks", {})
                }
                self.assertEqual(
                    {"config-ui", "mcp-auth", "mapp-mcp"}, control_members
                )

    def test_the_authorization_component_waits_for_its_database(self) -> None:
        """Its credentials live in the control schema, so it cannot start first.

        Only the bundled model has a `db` service to depend on; the external
        model has none, and there the health check is the only gate.
        """
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                services = model["services"]
                if "db" not in services:
                    self.assertNotIn("db", services["mcp-auth"].get("depends_on", {}))
                    continue
                self.assertEqual(
                    "service_healthy",
                    services["mcp-auth"]["depends_on"]["db"]["condition"],
                )

    def test_the_operator_credential_is_never_passed_as_an_environment_value(
        self,
    ) -> None:
        """It is read from control.admin_credential, not injected.

        MCP_AUTH_ADMIN_PASSWORD_HASH was consumed by the consent screen and set
        by nothing -- not compose, not .env.example, not ./bin/mapp -- so the
        deployed component could authenticate nobody, and `doctor`'s .env drift
        check could not report a key that was never documented.
        """
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                for name, service in model["services"].items():
                    self.assertNotIn(
                        "MCP_AUTH_ADMIN_PASSWORD_HASH",
                        service.get("environment", {}),
                        f"{name} still carries the retired credential variable",
                    )

    def test_production_clears_the_insecure_transport_escape_hatch(self) -> None:
        """compose.yaml claims a test pins this. This is that test.

        authlib refuses http:// for anything but literal localhost, and the
        development origin is http://mcp.localhost, so AUTHLIB_INSECURE_TRANSPORT
        is set in development. Carrying it into production would disable the
        check that keeps tokens off plaintext transports.
        """
        for mode in ("external", "bundled"):
            environment = self.models[mode]["services"]["mcp-auth"]["environment"]
            self.assertEqual("1", environment["AUTHLIB_INSECURE_TRANSPORT"], mode)
        for mode in ("external-production", "bundled-production"):
            environment = self.models[mode]["services"]["mcp-auth"]["environment"]
            self.assertIn(
                environment.get("AUTHLIB_INSECURE_TRANSPORT"),
                (None, ""),
                f"{mode} must clear AUTHLIB_INSECURE_TRANSPORT",
            )

    def test_the_configuration_api_can_reach_the_authorization_component(
        self,
    ) -> None:
        """Both halves of token-B validation, and the audience they share.

        The configuration API authenticates to the control listener as a
        confidential client and checks the token's audience on every
        introspection. If the two services resolved MCP_CONFIG_API_RESOURCE
        differently, every exchanged credential would be refused with nothing
        to indicate why -- so the two values are asserted equal rather than
        assumed to come from one expression.
        """
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                services = model["services"]
                config = services["config-ui"]["environment"]
                component = services["mcp-auth"]["environment"]
                self.assertEqual(
                    component["MCP_CONFIG_API_RESOURCE"],
                    config["MCP_CONFIG_API_RESOURCE"],
                )
                # Reached by service name on the control network they share.
                self.assertEqual(
                    "http://mcp-auth:8080", config["MCP_AUTH_URL"]
                )
                self.assertTrue(config["MCP_AUTH_CLIENT_ID"])

    def test_the_component_client_secret_is_not_shared_more_widely(self) -> None:
        """Only the configuration API needs it, so only it should have it."""
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                holders = {
                    name
                    for name, service in model["services"].items()
                    if "MCP_AUTH_CLIENT_SECRET" in service.get("environment", {})
                }
                self.assertEqual({"config-ui"}, holders)

    def test_the_runtime_client_secret_reaches_only_the_runtime(self) -> None:
        """One service: the one that presents it.

        An operator registers the MCP runtime's client with
        `./bin/mapp mcp-runtime-register`, which mints the secret and stores
        only its digest, so the authorization component never holds the
        plaintext even though it is the component that verifies it. An earlier
        version had that component provision the row from its own environment,
        which put the secret in a second configuration, made rotation a restart
        of the authorization server, and left the act without an author.
        """
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                holders = {
                    name
                    for name, service in model["services"].items()
                    if "MAPP_MCP_CLIENT_SECRET" in service.get("environment", {})
                }
                self.assertLessEqual(
                    holders,
                    {"mapp-mcp"},
                    "only the runtime presents this secret; nothing else needs it",
                )

    def test_the_mcp_origin_reaches_caddy_and_the_component_together(self) -> None:
        """One MCP_SITE, or the identifiers move without the site serving them.

        The component derives its issuer and resource from MCP_SITE; Caddy
        serves that origin. Caddy was never given the variable in the base
        model, so changing it moved the OAuth identifiers and left the site
        answering on the old hostname.
        """
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                services = model["services"]
                site = services["caddy"]["environment"]["MCP_SITE"]
                self.assertTrue(site)
                self.assertEqual(
                    site, services["mcp-auth"]["environment"]["MCP_ISSUER"]
                )
                self.assertEqual(
                    site + "/mcp",
                    services["mcp-auth"]["environment"]["MCP_RESOURCE"],
                )

    def test_the_published_edge_port_matches_the_port_the_sites_carry(self) -> None:
        """The edge must answer on the port its own origins name.

        Nothing asserted this, and a real defect lived in the gap: HTTP_PORT
        was 3000 while every site carried no port, so anything following an
        absolute URL built from an origin arrived at port 80 and found
        nothing. Browsers never noticed -- the dashboard's links are relative
        -- and the MCP authorization server's metadata document, which
        publishes absolute endpoint URLs a client is required to follow, was
        the first thing on the platform that could not tolerate it.

        Asserted from the resolved model rather than from .env, so it holds
        for whatever the deployment actually resolves to, and across all four
        models including the production overlays -- whose sites are https and
        so name 443. That is only possible because the development ports are
        the standard ones too; while HTTPS_PORT was 3443 the production models
        resolved a production overlay against development ports and could not
        satisfy this, which was itself the same defect one layer down.
        """
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                caddy = model["services"]["caddy"]
                published = {
                    str(entry["published"]): str(entry["target"])
                    for entry in caddy.get("ports", [])
                }
                self.assertTrue(published, "the edge publishes nothing")
                for key in ("MAP_SITE", "CONFIG_SITE", "MCP_SITE"):
                    site = caddy["environment"].get(key)
                    if not site:
                        continue
                    parts = urlsplit(site)
                    port = parts.port or (443 if parts.scheme == "https" else 80)
                    self.assertIn(
                        str(port),
                        published,
                        f"{key} is {site}, which names port {port}, but the"
                        f" edge publishes {sorted(published)}. A client"
                        " following an absolute URL from this origin would"
                        " reach nothing.",
                    )

    def test_gemini_credential_is_available_only_to_config_ui(self) -> None:
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                services = model["services"]
                self.assertIn(
                    "GEMINI_APIKEY",
                    services["config-ui"].get("environment", {}),
                )
                self.assertEqual(
                    "gemini-3.6-flash",
                    services["config-ui"]["environment"]["GEMINI_MODEL"],
                )
                for name, service in services.items():
                    if name == "config-ui":
                        continue
                    environment = service.get("environment", {})
                    self.assertNotIn("GEMINI_APIKEY", environment, name)
                    self.assertNotIn("GEMINI_MODEL", environment, name)

    def test_federation_credential_reaches_only_config_ui_and_the_database(
        self,
    ) -> None:
        """The provisioner credential is a database credential, not a service one.

        It belongs to the packaged database overlay and to the two services
        that need it. The base model carries no database credentials at all,
        and no other service -- xyz, caddy, browser-runner, the egress proxy --
        may see one, because a credential able to CREATE SERVER should not be
        readable from a process that only serves tiles.
        """
        for mode in ("external", "external-production"):
            with self.subTest(mode=mode):
                environment = self.models[mode]["services"]["config-ui"].get(
                    "environment", {}
                )
                self.assertNotIn("FEDERATION_DATABASE_URL", environment)
                self.assertNotIn("FEDERATION_DB_USER", environment)

        for mode in ("bundled", "bundled-production"):
            with self.subTest(mode=mode):
                services = self.models[mode]["services"]
                config_environment = services["config-ui"]["environment"]
                database_environment = services["db"]["environment"]
                self.assertEqual(
                    "mapp_federation",
                    config_environment["FEDERATION_DB_USER"],
                )
                self.assertIn(
                    "mapp_federation",
                    config_environment["FEDERATION_DATABASE_URL"],
                )
                self.assertIn(
                    "FEDERATION_DB_PASSWORD", database_environment
                )
                for name, service in services.items():
                    if name in {"config-ui", "db"}:
                        continue
                    environment = service.get("environment", {})
                    self.assertNotIn("FEDERATION_DATABASE_URL", environment)
                    self.assertNotIn("FEDERATION_DB_PASSWORD", environment)

    def test_the_demo_overlay_is_applied_only_when_it_is_switched_on(
        self,
    ) -> None:
        """The demo overlay must stay behind MAPP_DEMO_SOURCES in both files.

        compose.federated-demo.yaml declares nine required variables, so
        applying it unconditionally would make every one of them mandatory for
        every install -- including for `down`, `ps` and `logs`. bin/mapp and
        verify.sh must agree, because verify resolves the model it audits and
        would otherwise report a healthy demo deployment as stale.
        """
        for relative_path in ("bin/mapp", "scripts/verify.sh"):
            with self.subTest(path=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                guard = source.index('demo_sources="$(dotenv_value MAPP_DEMO_SOURCES)"')
                conditional = source.index(
                    'if [[ -n "${demo_sources}" ]]; then', guard
                )
                end = source.index("\nfi\n", conditional)
                block = source[conditional:end]
                self.assertIn("compose.federated-demo.yaml", block)
                self.assertIn("census-db ops-db", block)

    def test_the_demo_provisions_before_it_profiles(self) -> None:
        """Profiling reads foreign tables that provisioning creates.

        layers.sh had these the other way round, which could never have worked:
        source_<alias> does not exist until provision() creates it, and
        provision() is also what grants the consumer roles access to it. Every
        call in that script swallowed its errors, so the step failed silently on
        every run and the script still reported success.
        """
        source = (ROOT / "docker/demo-sources/layers.sh").read_text(
            encoding="utf-8"
        )

        self.assertLess(
            source.index('step "Provisioning'),
            source.index('step "Profiling'),
        )

    def test_the_demo_applies_the_drafts_it_generates(self) -> None:
        """/api/semantic/generate persists nothing; its own response says so.

        It answers with "proposalCreated": false. A describe step that stops
        there spends one model call per field, leaves the catalogue exactly as
        it found it, and prints that it described them -- which is what the
        first version of this step did. The draft only reaches the catalogue
        once it is checked, proposed and applied, and applying needs the two
        semantic proposal scopes on the minted token.
        """
        source = (ROOT / "docker/demo-sources/layers.sh").read_text(
            encoding="utf-8"
        )

        for required in (
            '"/api/semantic/proposals/check"',
            '"/api/semantic/proposals",',
            '/apply" % proposal["id"]',
            '"semantic:propose"',
            '"semantic:apply"',
        ):
            self.assertIn(required, source)

    def test_browser_egress_is_only_available_through_allowlisting_proxy(self) -> None:
        expected_allowlist = str(
            (ROOT / "instance/browser-egress-allowlist.txt").resolve()
        )
        expected_config = str(
            (ROOT / "docker/egress-proxy/squid.conf").resolve()
        )
        for mode, model in self.models.items():
            with self.subTest(mode=mode):
                services = model["services"]
                browser = services["browser-runner"]
                proxy = services["egress-proxy"]

                self.assertEqual(
                    {"automation"},
                    set(browser.get("networks", {})),
                )
                self.assertEqual(
                    {"automation", "browser-egress"},
                    set(proxy.get("networks", {})),
                )
                self.assertEqual(
                    {"egress-proxy"},
                    {
                        name
                        for name, service in services.items()
                        if "browser-egress" in service.get("networks", {})
                    },
                )
                self.assertEqual(
                    "http://egress-proxy:3128",
                    browser["environment"]["BROWSER_PROXY_SERVER"],
                )
                self.assertEqual(
                    "service_healthy",
                    browser["depends_on"]["egress-proxy"]["condition"],
                )
                self.assertTrue(proxy["read_only"])
                self.assertFalse(proxy.get("ports"))
                self.assertEqual("13:13", proxy["user"])
                self.assertEqual(["ALL"], proxy["cap_drop"])
                mounts = {
                    volume["target"]: volume
                    for volume in proxy.get("volumes", [])
                }
                self.assertEqual(expected_config, mounts["/etc/squid/squid.conf"]["source"])
                self.assertTrue(mounts["/etc/squid/squid.conf"]["read_only"])
                self.assertEqual(
                    expected_allowlist,
                    mounts["/etc/squid/browser-egress-allowlist.txt"]["source"],
                )
                self.assertTrue(
                    mounts["/etc/squid/browser-egress-allowlist.txt"]["read_only"]
                )

        allowlist = (ROOT / "instance/browser-egress-allowlist.txt").read_text(
            encoding="utf-8"
        )
        allowed_hosts = {
            line.strip()
            for line in allowlist.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn(".tile.openstreetmap.org", allowed_hosts)
        self.assertIn("cdn.jsdelivr.net", allowed_hosts)
        self.assertIn("geolytix.github.io", allowed_hosts)
        self.assertNotIn(".jsdelivr.net", allowed_hosts)
        self.assertNotIn(".github.io", allowed_hosts)

        squid = (ROOT / "docker/egress-proxy/squid.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'acl allowed_destinations dstdomain -n '
            '"/etc/squid/browser-egress-allowlist.txt"',
            squid,
        )
        hostname_deny = squid.index("http_access deny !allowed_destinations")
        address_deny = squid.index("http_access deny blocked_destinations")
        allow = squid.index("http_access allow CONNECT allowed_destinations")
        self.assertLess(hostname_deny, address_deny)
        self.assertLess(address_deny, allow)
        self.assertLess(allow, squid.index("http_access deny all"))
        self.assertIn("http_access deny CONNECT !SSL_ports", squid)
        self.assertIn("http_access deny !CONNECT", squid)
        self.assertNotIn("acl Safe_ports port 80", squid)
        self.assertIn("access_log none", squid)

    def test_census_loader_keeps_the_existing_bundled_etl_boundary(self) -> None:
        self.assertNotIn("etl", self.models["external"]["services"])
        self.assertNotIn("etl", self.models["external-production"]["services"])

        expected_config = str((ROOT / "instance/etl").resolve())
        for mode in ("bundled", "bundled-production"):
            with self.subTest(mode=mode):
                service = self.models[mode]["services"]["etl"]
                self.assertEqual("/config/census.json", service["environment"]["ETL_CENSUS_CONFIG"])
                self.assertTrue(service["read_only"])
                self.assertEqual({"backend"}, set(service.get("networks", {})))
                self.assertFalse(service.get("ports"))
                self.assertEqual(["ALL"], service["cap_drop"])
                self.assertIn("no-new-privileges:true", service["security_opt"])

                volumes = service.get("volumes", [])
                self.assertEqual(1, len(volumes))
                self.assertEqual("bind", volumes[0].get("type"))
                self.assertEqual(expected_config, volumes[0].get("source"))
                self.assertEqual("/config", volumes[0].get("target"))
                self.assertTrue(volumes[0].get("read_only"))


if __name__ == "__main__":
    unittest.main()
