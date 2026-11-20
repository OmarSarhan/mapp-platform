#!/usr/bin/env python3
"""Complete capabilities tool probe against an explicitly disposable database.

Uses real OAuth and config HTTP handlers and the registered MCP tool function.
Does not exercise MCP transport or a deployed stack. Creates fixture records;
never deletes control tables. Run against a fresh isolated database only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import subprocess
import types
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BASELINE_REF = "47d373770b92dd929b82fe3bbc26b18ce07abc22"
for directory in ("config-ui", "mcp-auth", "mapp-mcp"):
    sys.path.append(str(ROOT / directory))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable", action="store_true", required=True)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--baseline", action="store_true", help="load the three original modules from the verified OMA-76 baseline")
    args = parser.parse_args()
    dsn = os.environ["CONTROL_TEST_DATABASE_URL"]
    os.environ["CONTROL_DATABASE_URL"] = dsn
    os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"
    import psycopg
    import control_schema
    import control_connections
    import sql_store
    from models import Client, Grant, Token
    from issuer import MappAuthorizationServer
    from server import ControlServer
    from http.server import ThreadingHTTPServer
    from authentication import Authenticated, CURRENT_CALLER
    from introspection_client import IntrospectionClient
    from exchange_client import ExchangeClient
    from config_api_client import ConfigApiClient
    from protected_resource import ProtectedResource
    from runtime import build_runtime
    from mcp_token_client import McpTokenClient

    with control_schema.connect(dsn) as conn:
        control_schema.migrate(conn)
        if conn.execute("SELECT count(*) AS n FROM control.oauth_clients").fetchone()["n"]:
            raise SystemExit("Refusing a nonempty fixture database; create a fresh disposable database.")

    counts = {"connections": 0, "statements": 0}
    lock = threading.Lock()

    class CountedConnection(psycopg.Connection):
        @classmethod
        def connect(cls, *a, **kw):
            result = super().connect(*a, **kw)
            with lock:
                counts["connections"] += 1
            return result

        def execute(self, *a, **kw):
            with lock:
                counts["statements"] += 1
            return super().execute(*a, **kw)

    psycopg.connect = CountedConnection.connect
    # Independent service pools, as in the two deployed processes.
    oauth_pool = control_connections.ControlConnections()
    config_pool = control_connections.ControlConnections()
    sql_store.CONTROL_CONNECTIONS = oauth_pool
    control_connections.CONTROL_CONNECTIONS = config_pool
    # The connector is passed explicitly so fixture instrumentation also sees
    # config leases (the production default was captured at import time).
    original_lease = config_pool.connection
    config_pool.connection = lambda dsn, **kw: original_lease(dsn, connect=psycopg.connect, **kw)
    with tempfile.TemporaryDirectory(prefix="oma76-probe-") as directory:
        os.environ["CONTROL_DIR"] = directory + "/control"
        os.environ["WORKSPACE_PATH"] = directory + "/workspace.json"
        Path(os.environ["WORKSPACE_PATH"]).write_text('{"locales":{}}')
        import app
        if args.baseline:
            def original(path, name):
                module = types.ModuleType(name)
                module.__file__ = str(ROOT / path)
                sys.modules[name] = module
                source = subprocess.check_output(["git", "show", BASELINE_REF + ":" + path], cwd=ROOT)
                exec(compile(source, module.__file__, "exec"), module.__dict__)
                return module
            import server
            sql_store.SqlStore = original("mcp-auth/sql_store.py", "baseline_sql_store").SqlStore
            server.introspection = original("mcp-auth/introspection.py", "baseline_introspection")
            app.CONTROL = original("config-ui/control_plane.py", "baseline_control_plane").ControlStore(Path(directory) / "control")
        app.CONTROL.initialize(secrets.token_urlsafe(32), "oma76-fixture")
        store = sql_store.SqlStore(dsn)
        secret = secrets.token_urlsafe(32)
        raw = "mapp_a_" + secrets.token_urlsafe(32)
        resource = "http://mcp.localhost/mcp"
        config_resource = "http://config.localhost/api"
        store.add_client(Client("probe", "Probe", (), ("mcp:connect", "inspect")))
        store.add_client(Client("broker", "Broker", (), (),
                                token_endpoint_auth_method="client_secret_basic",
                                client_secret=secret,
                                capabilities=("exchange", "introspect", "redeem")))
        store.save_grant(Grant("oauth:probe", "probe", "operator", ("mcp:connect", "inspect")))
        store.save_token(raw, Token("", "probe", "mcp:connect inspect", "oauth:probe",
                                    int(time.time()), 3600, audience=resource))
        authorization = MappAuthorizationServer(store, issuer="http://mcp.localhost",
            resource=resource, config_api_resource=config_resource,
            scopes_supported=("mcp:connect", "inspect"), secure_cookies=False)
        auth = ControlServer(("127.0.0.1", 0), authorization)
        endpoint = f"http://127.0.0.1:{auth.server_port}"
        app.MCP_TOKENS = McpTokenClient(endpoint, "broker", secret, resource=config_resource)
        app.CONFIG_API_RESOURCE = config_resource
        config = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        config_endpoint = f"http://127.0.0.1:{config.server_port}"
        servers = (auth, config)
        threads = [threading.Thread(target=s.serve_forever) for s in servers]
        for thread in threads:
            thread.start()
        try:
            introspector = IntrospectionClient(endpoint, client_id="broker", client_secret=secret, resource=resource)
            exchanger = ExchangeClient(broker_endpoint=endpoint, config_api_endpoint=config_endpoint,
                config_api_resource=config_resource, client_id="broker", client_secret=secret)
            runtime = build_runtime(resource=ProtectedResource(origin="http://mcp.localhost", issuer="http://mcp.localhost"),
                exchange=exchanger, config_api=ConfigApiClient(endpoint=config_endpoint), config_api_resource=config_resource)
            tool = runtime._tool_manager._tools["capabilities_list"].fn

            def call():
                start = time.monotonic()
                token = None
                try:
                    token = CURRENT_CALLER.set(Authenticated(introspector.introspect(raw), raw))
                    result = tool()
                    assert result["actions"]
                    return {"seconds": time.monotonic() - start, "error": None}
                except Exception as exc:
                    return {"seconds": time.monotonic() - start, "error": type(exc).__name__,
                            "code": getattr(exc, "detail", {}).get("code")}
                finally:
                    if token is not None:
                        CURRENT_CALLER.reset(token)

            output = []
            oauth_pool.close()
            config_pool.close()
            for label, workers in (("cold", 1), ("warm", 1), ("concurrent", args.concurrency)):
                before = dict(counts)
                queued = oauth_pool.queue_seconds + config_pool.queue_seconds
                execution = oauth_pool.database_seconds + config_pool.database_seconds
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    results = list(executor.map(lambda _: call(), range(workers)))
                output.append({"path": label, "calls": results,
                    **{key: counts[key] - before[key] for key in counts},
                    "queueSeconds": oauth_pool.queue_seconds + config_pool.queue_seconds - queued,
                    "databaseSeconds": oauth_pool.database_seconds + config_pool.database_seconds - execution})
            return output
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()
            oauth_pool.close()
            config_pool.close()


if __name__ == "__main__":
    # Handler and tool logs are not part of the machine-readable result.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = main()
    print(json.dumps(result, indent=2))
