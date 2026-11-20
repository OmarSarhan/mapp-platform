"""HTTP and MCP outcome checks on a private socket-only PG18 on port 55485."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlencode

import psycopg

from wire_mutation_admission import fixture_database, run


@contextmanager
def occupied(count, lease):
    ready = threading.Barrier(count + 1, timeout=5)
    release = threading.Event()

    def hold():
        with lease():
            ready.wait()
            assert release.wait(10)

    with ThreadPoolExecutor(count) as workers:
        jobs = [workers.submit(hold) for _ in range(count)]
        try:
            ready.wait()
            yield
        finally:
            release.set()
            for job in jobs:
                job.result(5)


def check_wire(app, store, oauth, ex, cli, token_a, secret, config_port, auth_port, dsn, root):
    import control_connections as cc
    import control_plane
    import runtime_database as readers
    import server as broker
    from exchange_client import ExchangeUnavailable

    def request(port, method, path, body=None, headers=None):
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=4)) as conn:
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            raw = response.read()
            assert len(raw) == int(response.getheader("Content-Length"))
            return response.status, response.headers, raw

    def config(path, body=None, *, busy=False):
        request_id = secrets.token_hex(16)
        started = time.monotonic()
        status, headers, raw = request(config_port, "POST" if body else "GET", path,
                                    json.dumps(body) if body else None,
                                    {"Authorization": "Bearer " + cli, "X-Request-ID": request_id,
                                     "Content-Type": "application/json", "X-Mapp-Timeout-Seconds": ".25"})
        result = json.loads(raw)
        assert headers["X-Request-ID"] == result["meta"]["requestId"] == request_id
        if busy:
            elapsed = time.monotonic() - started
            assert elapsed < .45, (path, elapsed)
            assert status == 503 and headers["Retry-After"] == "1", status
            assert (result["code"], result["retryable"], result["indeterminate"]) == ("database.busy", True, False)
            assert result["mutation"]["targetStarted"] is False
            timing = result["meta"]["databaseTiming"]
            assert .1 < timing["queueSeconds"] < .45 and timing["databaseSeconds"] >= 0
            print(f"{path} 503 elapsed={elapsed:.3f}s queue={timing['queueSeconds']:.3f}s db={timing['databaseSeconds']:.3f}s")
        else:
            assert status == 200, (path, status, result.get("code"))
        return result

    identity = "/api/public/identity"
    with occupied(cc.CAPACITY, lambda: cc.CONTROL_CONNECTIONS.connection(dsn)):
        config(identity, busy=True)
    assert config(identity)["instanceId"]

    # PG18 here has no PostGIS. Provide the three catalog signatures used
    # for column discovery, with no spatial data in this disposable DB.
    with psycopg.connect(dsn, user="postgres", autocommit=True) as owner:
        owner.execute("CREATE DOMAIN public.geometry AS bytea")
        owner.execute("CREATE FUNCTION public.postgis_typmod_type(int) RETURNS text LANGUAGE sql AS 'SELECT NULL::text'")
        owner.execute("CREATE FUNCTION public.postgis_typmod_srid(int) RETURNS int LANGUAGE sql AS 'SELECT NULL::int'")
    with psycopg.connect(dsn) as db:
        db.execute("CREATE TABLE control.measurements (value integer)")
        db.execute("INSERT INTO control.measurements VALUES (7)")
    workspace = {"key": "demo", "dbs": "demo", "locale": {"layers": {
        "read": {"table": "control.measurements", "dbs": "demo"}}}}
    app.WORKSPACE.write_text(json.dumps(workspace))
    query = {"layer": "read", "expression": "value", "type": "integer"}
    with patch.dict(app.DB_CONNECTIONS, demo=dsn):
        with occupied(readers.MAX_CONCURRENT_DBS_CONNECTIONS,
                      lambda: readers.dbs_connection(psycopg.connect, dsn, user="postgres")):
            config("/api/catalog", busy=True)
            config("/api/sql/test", query, busy=True)
        config("/api/catalog")
        assert config("/api/sql/test", query)["valid"]
    print("identity, catalog, authenticated SQL test: complete timed 503 while held, fresh 200")

    resource = "http://mcp.localhost/mcp"
    def introspect(token, audience):
        return request(auth_port, "POST", "/internal/oauth/introspect",
                    urlencode({"token": token, "resource": audience}),
                    {"Authorization": app.MCP_TOKENS._authorization,
                     "Content-Type": "application/x-www-form-urlencoded", "X-Mapp-Timeout-Seconds": ".2"})

    with psycopg.connect(dsn) as db:
        db.execute("LOCK control.oauth_tokens IN ACCESS EXCLUSIVE MODE")
        status, headers, raw = introspect(token_a, resource)
        assert status == 503 and headers["Retry-After"] == "1" and json.loads(raw)["error"] == "temporarily_unavailable"
    assert json.loads(introspect(token_a, resource)[2])["active"]
    inactive = introspect(token_a, app.CONFIG_API_RESOURCE)
    assert inactive[0] == 200 and json.loads(inactive[2]) == {"active": False}
    assert introspect("unknown", resource)[2] == inactive[2]

    args = dict(operation_id="xyz.reload", method="POST", path_template="/api/xyz/reload",
                path="/api/xyz/reload", query="", body={"confirmed": True})
    def exchange():
        return ex.exchange(subject_token=token_a, scope="reload", **args)

    with patch.object(broker.exchange, "exchange", side_effect=RuntimeError("fixture outage")):
        try:
            exchange()
        except ExchangeUnavailable:
            pass
        else:
            raise AssertionError("broker 500 lost its type")
    token = exchange()
    with patch.object(broker.introspection, "redeem", side_effect=RuntimeError("fixture outage")):
        status, _, raw = request(config_port, "POST", args["path"], json.dumps(args["body"]),
                              {"Authorization": "Bearer " + token, app.APPROVAL_RECEIPT_HEADER: "fixture"})
        error = json.loads(raw)
        assert status == 503 and error["mutation"]["tokenB"] == "unconfirmed" and not error["retryable"]
        assert oauth.exchanged_binding(token)["consumed_at"] is None
    assert app.MCP_TOKENS.redeem(token, args["operation_id"], ex.request_digest(**args))["redeemed"]
    decode = app.MCP_TOKENS._decode
    def expire_on_ack(*values, **options):
        payload = decode(*values, **options)
        if payload.get("redeemed") is True:
            cc.current_request().cancel("fixture expiry after broker acknowledgement")
        return payload

    token = exchange()
    with patch.object(app.MCP_TOKENS, "_decode", expire_on_ack):
        status, _, raw = request(config_port, "POST", args["path"], json.dumps(args["body"]),
                                 {"Authorization": "Bearer " + token, app.APPROVAL_RECEIPT_HEADER: "fixture"})
        error = json.loads(raw)
        assert status == 503 and error["mutation"]["tokenB"] == "spent" and not error["mutation"]["targetStarted"]
        assert oauth.exchanged_binding(token)["consumed_at"] is not None
    print("broker SQL wait and exchange/redemption 500: unavailable, valid credentials recovered")

    store.open_approval_window(client_id="fixture", instance=store.instance_id(),
                               created_by="fixture", session_created_at=control_plane.now())
    source = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(source / "mapp-mcp/tests"))
    from wire_origin_disconnect import call
    sock = str(root / "rpc")
    env = dict(os.environ, MCP_RUNTIME_SOCKET=sock, MCP_SITE="http://mcp.localhost",
               MCP_AUTH_URL=ex.broker_endpoint, MCP_CONFIG_API_URL=ex.config_api_endpoint,
               MAPP_MCP_CLIENT_ID="broker", MAPP_MCP_CLIENT_SECRET=secret, PYTHONDONTWRITEBYTECODE="1")
    with subprocess.Popen([sys.executable, str(source / "mapp-mcp/server.py")], env=env) as proc:
        try:
            end = time.monotonic() + 10
            while not Path(sock).exists():
                assert proc.poll() is None and time.monotonic() < end
                time.sleep(.01)

            def rpc_reload():
                with closing(call(sock, token_a, name="xyz_reload")) as conn:
                    raw = conn.getresponse().read().decode()
                frames = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
                return json.loads(frames[-1] if frames else raw)["result"]

            def interrupted_reload():
                result = rpc_reload()
                assert result["isError"]
                return result["structuredContent"]["error"]

            audit = store.audit
            def after_spend(event, **kwargs):
                if event == "approval.consumed":
                    raise cc.CommandNotSubmitted("fixture later command")
                return audit(event, **kwargs)

            requested = root / "reload/requested"
            before = int(requested.read_text())
            with patch.object(store, "audit", after_spend):
                err = interrupted_reload()
            facts = err["mutation"]
            assert (err["retryable"], err["indeterminate"], facts["targetStarted"]) == (False, False, False)
            assert facts["receipt"] == "consumed" and facts["tokenB"] == "spent"
            recovery = facts["recovery"]
            record = store.read_approval_by_reference(recovery["approvalReference"])
            assert record["recovery"]["operationId"] == recovery["operationId"]
            assert int(requested.read_text()) == before and not (store.operations / (recovery["operationId"] + ".json")).exists()

            with patch.object(app, "wait_reload", side_effect=cc.CommandNotSubmitted("fixture later command")):
                err = interrupted_reload()
            facts = err["mutation"]
            assert (err["retryable"], err["indeterminate"], facts["targetStarted"]) == (False, True, True)
            assert err["databaseTiming"]["queueSeconds"] >= 0 and err["databaseTiming"]["databaseSeconds"] > 0
            assert facts["published"]["requestedGeneration"] == int(requested.read_text()) == before + 1
            recovery = facts["recovery"]
            assert store.read_operation(recovery["operationId"])["status"] == "indeterminate"
            assert store.read_approval_by_reference(recovery["approvalReference"])["recovery"]["requestId"] == err["requestId"]
            print("Unix MCP reload before/after publication: receipt, operation, request ID and generation verified")

            def expire_final_audit(event, **kwargs):
                if event == "xyz.reload_requested":
                    cc.current_request().cancel("fixture expiry after terminal acknowledgement")
                return audit(event, **kwargs)

            generation = before + 2
            with patch.object(store, "audit", expire_final_audit), ThreadPoolExecutor(1) as workers:
                result = workers.submit(rpc_reload)
                end = time.monotonic() + 3
                while int(requested.read_text()) != generation:
                    assert time.monotonic() < end
                    time.sleep(.005)
                (requested.parent / "workspace-fingerprint").write_bytes((requested.parent / "expected-workspace").read_bytes())
                (requested.parent / "applied").write_text(str(generation))
                ack = result.result(3)
            assert not ack["isError"]
            acknowledged = ack.get("structuredContent") or json.loads(ack["content"][0]["text"])
            operation = acknowledged["operation"]
            assert operation["status"] == store.read_operation(operation["id"])["status"] == "succeeded"
            assert acknowledged["mutation"]["published"]["requestedGeneration"] == int(requested.read_text()) == generation
            print("Unix MCP acknowledged reload survived expiry in later file audit")
        finally:
            proc.terminate()
            proc.wait(5)

    token = exchange()
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("UPDATE control.oauth_clients SET disabled_at=now() WHERE client_id='fixture'")
        assert introspect(token, app.CONFIG_API_RESOURCE)[2] == inactive[2]
        db.execute("UPDATE control.oauth_clients SET disabled_at=NULL WHERE client_id='fixture'")
    assert json.loads(introspect(token, app.CONFIG_API_RESOURCE)[2])["active"]
    assert oauth.revoke_grant("oauth:fixture")
    assert introspect(token, app.CONFIG_API_RESOURCE)[2] == inactive[2]
    print("unknown, wrong audience, disabled owner and revoked grant: identical inactive replies")


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("This fixture requires assertions.")
    with fixture_database(sys.argv[1], fixture="oma120") as dsn:
        with tempfile.TemporaryDirectory(dir=sys.argv[1]) as tmp:
            run(Path(tmp), dsn, check_wire=check_wire)
