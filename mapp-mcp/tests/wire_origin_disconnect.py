"""Standalone integration check on the private OMA-118 socket-only PG fixture.

Run from the repo root with the MCP and psycopg dependencies installed.
Requires a local postgres fixture admin and an oma118 login capped at eight.
Creates and drops one uniquely named database. No deployment DSNs are read.
"""
import argparse
import concurrent.futures
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import queue
import re
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


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


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
    scopes = ("mcp:connect", "inspect", "reload")
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
                observed.append((self.path, threading.current_thread()))
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
                        require(release.wait(10), "fixture lease release timed out")

                leases = [workers.submit(holder) for _ in range(3)]
                ready.wait(5)
                observed.clear()
                pipe.send("held")
                for expected in (1, 0):
                    require(recv(pipe) == "observe", "expected observe")
                    until = time.monotonic() + 0.5
                    while pool._waiters != expected and time.monotonic() < until:
                        time.sleep(0.002)
                    snapshot = observed.copy()
                    if expected == 1:
                        require(len(snapshot) == 1, "expected exactly one OAuth POST")
                        handler = snapshot[0][1]
                    pipe.send((pool._waiters, [path for path, _ in snapshot]))
                require(recv(pipe) == "release", "expected release")
                release.set()
                for lease in leases:
                    lease.result()
                handler.join(2)
                require(not handler.is_alive(), "observed OAuth handler did not exit")
                pipe.send("released and joined")


def config(env, pipe, dispatches):
    service_env("config-ui", env)
    from http.server import ThreadingHTTPServer
    from control_plane import ControlStore, now

    password = secrets.token_urlsafe(32)
    ControlStore(Path(env["CONTROL_DIR"])).initialize(password, "fixture")
    import app
    app.CONTROL.initialize(password, "fixture")
    app.CONTROL.open_approval_window(client_id="fixture", instance=app.CONTROL.instance_id(),
                                     created_by="fixture", session_created_at=now())
    handler_ready = queue.Queue()
    proceed = threading.Event()
    redeem = app.MCP_TOKENS.redeem

    def gated_redeem(token, operation_id, request_digest):
        if operation_id == "xyz.reload":
            handler_ready.put(threading.current_thread())
            require(proceed.wait(5), "config redemption gate timed out")
        return redeem(token, operation_id, request_digest)

    app.MCP_TOKENS.redeem = gated_redeem

    class CountTarget(app.Handler):
        def do_GET(self):
            if self.headers.get("X-Fixture-Drain") == "direct":
                handler_ready.put(threading.current_thread())
            if self.path == "/api/capabilities":
                with dispatches.get_lock():
                    dispatches.value += 1
            super().do_GET()

    with ThreadingHTTPServer(("127.0.0.1", 0), CountTarget) as httpd:
        pipe.send(httpd.server_port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        require(pipe.recv() == "await reload", "expected reload")
        handler = handler_ready.get(timeout=5)
        pipe.send("redeem ready")
        require(recv(pipe) == "proceed", "expected proceed")
        proceed.set()
        require(recv(pipe) == "join", "expected join")
        handler.join(2)
        require(not handler.is_alive(), "observed config handler did not exit")
        pipe.send("config joined")
        require(recv(pipe) == "join direct", "expected direct config request")
        handler = handler_ready.get(timeout=2)
        handler.join(2)
        require(not handler.is_alive(), "direct config handler did not exit")
        pipe.send("config joined")
        pipe.recv()  # continue serving fresh requests until fixture shutdown


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
            tag = dict(scope["headers"]).get(b"x-fixture-drain")
            if tag is None:
                return await app(scope, receive, send)
            barrier = drained[tag]
            try:
                await app(scope, receive, send)
            finally:
                # One fixture RPC at a time. Include workers that the
                # transport may still be unwinding after a disconnected POST.
                limiter = anyio.to_thread.current_default_thread_limiter()
                while limiter.borrowed_tokens or limiter.statistics().tasks_waiting:
                    await asyncio.sleep(.002)
                barrier.set()

        return observed, resource

    server.build_app = observed_app
    server.main()


def call(path, token, tag=None, name="capabilities_list"):
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(5)
    sock.connect(path)
    conn = http.client.HTTPConnection("mcp.localhost")
    conn.sock = sock
    params = {"name": name, "arguments": {}, "_meta": {
        "io.modelcontextprotocol/protocolVersion": VERSION,
        "io.modelcontextprotocol/clientCapabilities": {}}}
    conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": params}), {"Mcp-Protocol-Version": VERSION, "Mcp-Method": "tools/call",
                 "Mcp-Name": params["name"], "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token,
                 **({"X-Fixture-Drain": tag} if tag else {})})
    return conn


def healthy(path, token):
    conn = call(path, token)
    try:
        response = conn.getresponse()
        body = response.read().decode()
        frames = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        result = (frames or [json.loads(body)])[-1]["result"]
        require(response.status == 200 and not result.get("isError") and "actions" in json.dumps(result),
                "healthy RPC failed")
    finally:
        conn.close()


def recv(pipe):
    require(pipe.poll(10), "fixture service did not answer")
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
    endpoints = ("exchange", "introspect")
    drained = {name.encode(): ctx.Event() for name in (*endpoints, "redeem")}

    def start(fn, *args):
        proc = ctx.Process(target=fn, args=(env, *args))
        proc.start()
        processes.append(proc)

    def count_tokens():
        with psycopg.connect(dsn) as conn:
            return conn.execute("SELECT count(*) FROM control.oauth_tokens WHERE operation_id IS NOT NULL").fetchone()[0]

    def reload_state():
        with psycopg.connect(dsn) as observer:
            tokens = observer.execute(
                "SELECT single_use, consumed_at IS NOT NULL FROM control.oauth_tokens"
                " WHERE operation_id = 'xyz.reload' AND revoked_at IS NULL AND expires_at > now()").fetchall()
            receipts = observer.execute(
                "SELECT status, receipt_hash IS NOT NULL, consumed_at IS NOT NULL FROM control.approvals"
                " WHERE operation_id = 'xyz.reload' AND revoked_at IS NULL").fetchall()
        return (tokens, receipts, list(Path(env["CONTROL_DIR"], "operations").glob("*.json")),
                list(Path(env["RELOAD_DIR"]).glob("*")), workspace.read_bytes(), count_tokens())

    try:
        auth, child = ctx.Pipe()
        start(oauth, child, tokens)
        env["MCP_AUTH_URL"] = f"http://127.0.0.1:{recv(auth)}"
        config_pipe, child = ctx.Pipe()
        start(config, child, dispatches)
        config_port = recv(config_pipe)
        env["MCP_CONFIG_API_URL"] = f"http://127.0.0.1:{config_port}"
        start(runtime, drained)
        until = time.monotonic() + 10
        while not Path(path).exists() and time.monotonic() < until:
            time.sleep(0.01)
        healthy(path, tokens[0])
        for token, endpoint in zip(tokens, endpoints, strict=True):
            before = count_tokens(), dispatches.value, workspace.read_bytes()
            auth.send("hold")
            require(recv(auth) == "held", "leases were not held")
            conn = call(path, token, endpoint)
            try:
                auth.send("observe")
                require(recv(auth) == (1, ["/internal/oauth/" + endpoint]), "wrong OAuth waiter")
            finally:
                conn.close()
            started = time.monotonic()
            auth.send("observe")
            waiters, _ = recv(auth)
            elapsed = time.monotonic() - started
            auth.send("release")
            require(recv(auth) == "released and joined", "OAuth handler did not drain")
            require(drained[endpoint.encode()].wait(2), "MCP request or admitted worker did not drain")
            after = count_tokens(), dispatches.value, workspace.read_bytes()
            healthy(path, token)
            print(f"{endpoint} waiters={waiters} elapsed={elapsed:.3f}s tokens={before[0]}->{after[0]} "
                  f"dispatches={before[1]}->{after[1]} handler=joined request=drained fresh=ok", flush=True)
            require(elapsed < 0.6 and waiters == 0 and before == after, "disconnect invariant failed")

        conn = call(path, tokens[0], "redeem", "xyz_reload")
        try:
            config_pipe.send("await reload")
            require(recv(config_pipe) == "redeem ready", "config did not reach reload redemption")
            before = reload_state()
            require(before[:4] == ([(True, False)], [("approved", True, False)], [], []),
                    "expected one unspent token B, one claimed receipt and no target effect")
            auth.send("hold")
            require(recv(auth) == "held", "leases were not held")
            config_pipe.send("proceed")
            auth.send("observe")
            require(recv(auth) == (1, ["/internal/oauth/redeem"]), "wrong redemption waiter")
        finally:
            conn.close()
        started = time.monotonic()
        auth.send("observe")
        waiters, routes = recv(auth)
        elapsed = time.monotonic() - started
        auth.send("release")
        require(recv(auth) == "released and joined", "redemption handler did not drain")
        config_pipe.send("join")
        require(recv(config_pipe) == "config joined", "config did not drain")
        require(drained[b"redeem"].wait(2), "reload RPC did not drain")
        after = reload_state()
        healthy(path, tokens[0])
        print(f"redeem waiters={waiters} elapsed={elapsed:.3f}s tokensB(single_use,spent)={after[0]} "
              f"receipts={after[1]} operations={len(after[2])} reloadFiles={len(after[3])} "
              f"minted={before[5]}->{after[5]} "
              "handlers=joined request=drained fresh=ok", flush=True)
        require(routes == ["/internal/oauth/redeem"] and elapsed < 0.6 and waiters == 0
                and before == after, "config-to-OAuth disconnect invariant failed")

        auth.send("hold")
        require(recv(auth) == "held", "leases were not held")
        conn = http.client.HTTPConnection("127.0.0.1", config_port, timeout=5)
        try:
            conn.request("GET", "/api/capabilities", headers={
                "Authorization": "Bearer mapp_b_" + secrets.token_urlsafe(32), "X-Fixture-Drain": "direct"})
            auth.send("observe")
            require(recv(auth) == (1, ["/internal/oauth/introspect"]), "wrong direct config waiter")
        finally:
            conn.close()
        auth.send("observe")
        require(recv(auth) == (0, ["/internal/oauth/introspect"]), "direct peer left OAuth queued")
        auth.send("release")
        require(recv(auth) == "released and joined", "direct OAuth handler did not drain")
        config_pipe.send("join direct")
        require(recv(config_pipe) == "config joined", "direct config handler did not drain")
        healthy(path, tokens[0])
        print("direct config waiters=0 handlers=joined fresh=ok", flush=True)
    finally:
        for proc in reversed(processes):
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
                proc.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", default=SOCKET)
    parser.add_argument("--role", default="oma118")
    args = parser.parse_args()
    directory = str(Path(args.socket_dir).resolve())
    require(directory == SOCKET or re.fullmatch(r"/tmp/oma118-repair-pg-[A-Za-z0-9]+", directory),
            "expected a private OMA-118 fixture socket directory")
    require(re.fullmatch(r"oma118(?:_[a-f0-9]{16})?", args.role), "expected a fixture role")
    db = "oma118_" + secrets.token_hex(8)
    address = dict(host=directory, port=55478)
    with psycopg.connect(**address, dbname="postgres", user="postgres", autocommit=True) as admin:
        require(admin.execute("SHOW listen_addresses").fetchone() == ("",), "fixture must be Unix-only")
        require(admin.execute("SHOW unix_socket_directories").fetchone() == (directory,) and
                admin.execute("SHOW port").fetchone() == ("55478",), "unexpected fixture endpoint")
        role = admin.execute("SELECT rolsuper, rolcanlogin, rolconnlimit FROM pg_roles WHERE rolname=%s",
                             (args.role,)).fetchone()
        require(role == (False, True, 8), "expected a capped fixture login")
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
            sql.Identifier(db), sql.Identifier(args.role)))
        try:
            dsn = psycopg.conninfo.make_conninfo(**address, dbname=db, user=args.role)
            with psycopg.connect(dsn) as conn:
                conn.execute(sql.SQL("CREATE SCHEMA control AUTHORIZATION {}").format(sql.Identifier(args.role)))
            sys.path.insert(0, str(ROOT / "config-ui"))
            import control_schema
            with control_schema.connect(dsn) as conn:
                control_schema.migrate(conn)
            with tempfile.TemporaryDirectory(prefix="oma118-") as directory:
                exercise(directory, dsn)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db)))
