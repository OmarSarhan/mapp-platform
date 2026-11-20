"""Standalone integration check on the private OMA-118 socket-only PG fixture.

Run from the repo root with the MCP and psycopg dependencies installed.
Requires a local postgres fixture admin and an oma118 login capped at eight.
Creates and drops one uniquely named database. No deployment DSNs are read.
"""
import concurrent.futures
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[2]
VERSION = "2026-07-28"
SOCKET = "/tmp/oma118-pg"


def service_env(service, env):
    os.environ.clear()
    os.environ.update(env)
    sys.path.insert(0, str(ROOT / service))


def oauth(env, pipe, tokens):
    service_env("mcp-auth", env)
    import server
    from control_connections import CONTROL_CONNECTIONS as pool
    from models import Client, Grant, Token

    store = server.build_store()
    scopes = ("mcp:connect", "inspect")
    store.add_client(Client("fixture", "fixture", (), scopes))
    store.add_client(Client("runtime", "fixture", (), (), token_endpoint_auth_method="client_secret_basic",
                            client_secret=env["MAPP_MCP_CLIENT_SECRET"], capabilities=("introspect", "exchange")))
    store.save_grant(Grant("oauth:fixture", "fixture", "operator", scopes))
    for token in tokens:
        store.save_token(token, Token("", "fixture", " ".join(scopes), "oauth:fixture",
                                      int(time.time()), 3600, audience="http://mcp.localhost/mcp"))
    pool.close()
    with server.ControlServer(("127.0.0.1", 0), server.build_authorization(store)) as httpd:
        observed = []
        original = httpd.RequestHandlerClass

        class CountRoutes(original):
            def do_POST(self):
                observed.append(self.path)
                super().do_POST()

        httpd.RequestHandlerClass = CountRoutes
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        pipe.send(httpd.server_port)
        with concurrent.futures.ThreadPoolExecutor(3) as workers:
            while pipe.recv() == "hold":
                ready = threading.Barrier(4)
                release = threading.Event()

                def holder():
                    with pool.connection(env["CONTROL_DATABASE_URL"]):
                        ready.wait(5)
                        assert release.wait(10)

                leases = [workers.submit(holder) for _ in range(3)]
                ready.wait(5)
                observed.clear()
                pipe.send("held")
                for expected in (1, 0):
                    assert pipe.recv() == "observe"
                    until = time.monotonic() + 0.5
                    while pool._waiters != expected and time.monotonic() < until:
                        time.sleep(0.002)
                    pipe.send((pool._waiters, list(observed)))
                assert pipe.recv() == "release"
                release.set()
                for lease in leases:
                    lease.result()
                pipe.send("released")


def config(env, pipe, dispatches):
    service_env("config-ui", env)
    from http.server import ThreadingHTTPServer
    from control_plane import ControlStore

    password = secrets.token_urlsafe(32)
    ControlStore(Path(env["CONTROL_DIR"])).initialize(password, "fixture")
    import app
    app.CONTROL.initialize(password, "fixture")

    class CountTarget(app.Handler):
        def do_GET(self):
            if self.path == "/api/capabilities":
                with dispatches.get_lock():
                    dispatches.value += 1
            super().do_GET()

    with ThreadingHTTPServer(("127.0.0.1", 0), CountTarget) as httpd:
        pipe.send(httpd.server_port)
        httpd.serve_forever()


def runtime(env, drained):
    service_env("mapp-mcp", env)
    import asyncio
    import anyio
    import server

    build = server.build_app

    def observed_app():
        app, resource = build()

        async def observed(scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            drained.clear()
            try:
                await app(scope, receive, send)
            finally:
                # One fixture RPC at a time. Include workers that the
                # transport may still be unwinding after a disconnected POST.
                limiter = anyio.to_thread.current_default_thread_limiter()
                while limiter.borrowed_tokens or limiter.statistics().tasks_waiting:
                    await asyncio.sleep(.002)
                drained.set()

        return observed, resource

    server.build_app = observed_app
    server.main()


def call(path, token):
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(5)
    sock.connect(path)
    conn = http.client.HTTPConnection("mcp.localhost")
    conn.sock = sock
    params = {"name": "capabilities_list", "arguments": {}, "_meta": {
        "io.modelcontextprotocol/protocolVersion": VERSION,
        "io.modelcontextprotocol/clientCapabilities": {}}}
    conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": params}), {"Mcp-Protocol-Version": VERSION, "Mcp-Method": "tools/call",
                 "Mcp-Name": params["name"], "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token})
    return conn


def healthy(path, token):
    conn = call(path, token)
    try:
        response = conn.getresponse()
        body = response.read().decode()
        frames = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        result = (frames or [json.loads(body)])[-1]["result"]
        assert response.status == 200 and not result.get("isError") and "actions" in json.dumps(result)
    finally:
        conn.close()


def recv(pipe):
    assert pipe.poll(10), "fixture service did not answer"
    return pipe.recv()


def exercise(directory, dsn):
    ctx = multiprocessing.get_context("spawn")
    workspace = Path(directory, "workspace")
    workspace.write_text('{"locales":{}}')
    path = directory + "/rpc"
    env = dict(CONTROL_DATABASE_URL=dsn, CONTROL_DIR=directory + "/control",
               WORKSPACE_PATH=str(workspace), RELOAD_DIR=directory + "/reload", MCP_RUNTIME_SOCKET=path,
               MCP_CONFIG_API_RESOURCE="http://config.localhost/api", MCP_SITE="http://mcp.localhost",
               MCP_ISSUER="http://mcp.localhost", AUTHLIB_INSECURE_TRANSPORT="1",
               MCP_AUTH_CLIENT_ID="config", MCP_AUTH_CLIENT_SECRET=secrets.token_urlsafe(32),
               MAPP_MCP_CLIENT_ID="runtime", MAPP_MCP_CLIENT_SECRET=secrets.token_urlsafe(32))
    processes = []
    tokens = ["mapp_a_" + secrets.token_urlsafe(32) for _ in range(2)]
    dispatches = ctx.Value("i", 0)
    drained = ctx.Event()

    def start(fn, *args):
        proc = ctx.Process(target=fn, args=(env, *args))
        proc.start()
        processes.append(proc)

    def count_tokens():
        with psycopg.connect(dsn) as conn:
            return conn.execute("SELECT count(*) FROM control.oauth_tokens WHERE operation_id IS NOT NULL").fetchone()[0]

    try:
        auth, child = ctx.Pipe()
        start(oauth, child, tokens)
        env["MCP_AUTH_URL"] = f"http://127.0.0.1:{recv(auth)}"
        config_pipe, child = ctx.Pipe()
        start(config, child, dispatches)
        env["MCP_CONFIG_API_URL"] = f"http://127.0.0.1:{recv(config_pipe)}"
        start(runtime, drained)
        until = time.monotonic() + 10
        while not Path(path).exists() and time.monotonic() < until:
            time.sleep(0.01)
        healthy(path, tokens[0])
        for token, endpoint in zip(tokens, ("exchange", "introspect"), strict=True):
            before = count_tokens(), dispatches.value, workspace.read_bytes()
            auth.send("hold")
            assert recv(auth) == "held"
            conn = call(path, token)
            try:
                auth.send("observe")
                assert recv(auth) == (1, ["/internal/oauth/" + endpoint])
            finally:
                conn.close()
            started = time.monotonic()
            auth.send("observe")
            waiters, _ = recv(auth)
            elapsed = time.monotonic() - started
            auth.send("release")
            assert recv(auth) == "released"
            assert drained.wait(2), "MCP request or admitted worker did not drain"
            after = count_tokens(), dispatches.value, workspace.read_bytes()
            healthy(path, token)
            print(f"{endpoint} waiters={waiters} elapsed={elapsed:.3f}s tokens={before[0]}->{after[0]} "
                  f"dispatches={before[1]}->{after[1]} fresh=ok", flush=True)
            assert elapsed < 0.6 and waiters == 0 and before == after
    finally:
        for proc in reversed(processes):
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
                proc.join()


if __name__ == "__main__":
    db = "oma118_" + secrets.token_hex(8)
    address = dict(host=SOCKET, port=55478)
    with psycopg.connect(**address, dbname="postgres", user="postgres", autocommit=True) as admin:
        assert admin.execute("SHOW listen_addresses").fetchone() == ("",)
        assert admin.execute("SELECT rolsuper, rolconnlimit FROM pg_roles WHERE rolname='oma118'").fetchone() == (False, 8)
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER oma118 TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(db)))
        try:
            dsn = psycopg.conninfo.make_conninfo(**address, dbname=db, user="oma118")
            with psycopg.connect(dsn) as conn:
                conn.execute("CREATE SCHEMA control AUTHORIZATION oma118")
            sys.path.insert(0, str(ROOT / "config-ui"))
            import control_schema
            with control_schema.connect(dsn) as conn:
                control_schema.migrate(conn)
            with tempfile.TemporaryDirectory(prefix="oma118-") as directory:
                exercise(directory, dsn)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db)))
