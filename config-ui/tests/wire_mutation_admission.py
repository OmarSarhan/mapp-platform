"""Standalone regression using loopback HTTP and a private socket-only PG18.

Pass the /tmp/oma114-* socket directory of a private cluster on 55484
with a local postgres admin and an oma114 login limited to eight sessions.
Creates and drops a unique database. Never uses deployment environment DSNs.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import base64
import fcntl
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import psycopg
from psycopg import sql


@contextmanager
def fixture_database(directory, *, fixture="oma114"):
    directory = Path(directory).resolve()
    port = {"oma114": 55484, "oma120": 55485}[fixture]
    assert re.fullmatch(rf"/tmp/{fixture}-[a-zA-Z0-9]+", str(directory))
    conninfo = f"host={directory} port={port}"
    name = "mutation_" + secrets.token_hex(8)
    with psycopg.connect(conninfo, user="postgres", dbname="postgres", autocommit=True) as db:
        assert db.info.server_version // 10000 == 18
        assert db.execute("SHOW listen_addresses").fetchone() == ("",)
        assert Path(db.execute("SHOW data_directory").fetchone()[0]).parent == directory
        assert db.execute("SELECT rolconnlimit, rolsuper FROM pg_roles WHERE rolname=%s", (fixture,)).fetchone() == (8, False)
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(conninfo, user="postgres", dbname=name) as owner:
                owner.execute(sql.SQL("CREATE SCHEMA control AUTHORIZATION {}").format(sql.Identifier(fixture)))
            yield f"{conninfo} user={fixture} dbname={name}"
        finally:
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@contextmanager
def serving(server):
    with server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join(4)
            assert not thread.is_alive()


def run(root, dsn, *, check_wire=None):
    os.environ.clear()
    os.environ.update(CONTROL_DATABASE_URL=dsn, CONTROL_DIR=str(root / "control"),
                      WORKSPACE_PATH=str(root / "workspace.json"), RELOAD_DIR=str(root / "reload"),
                      TMPDIR=str(root), SVG_DIR=str(root / "svg"), AUTHLIB_INSECURE_TRANSPORT="1",
                      MCP_CONFIG_API_RESOURCE="http://config.localhost/api")
    tempfile.tempdir = str(root)
    source = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(source / directory) for directory in ("config-ui", "mcp-auth", "mapp-mcp")]
    import app
    import control_api
    import control_connections as cc
    import derived_drafts
    import sql_store
    from exchange_client import ExchangeClient
    from models import Client, Grant, Token
    from server import ControlServer, build_authorization

    reports = queue.Queue()

    class StopAfterReplace(BaseException):
        """Discard the handler stack as on process loss, before acknowledgement."""

    class Handler(app.Handler):
        def do_POST(self):
            try:
                super().do_POST()
            except (BrokenPipeError, ConnectionResetError, StopAfterReplace):
                pass
            finally:
                reports.put(self._effects)

        def log_message(self, *args):
            pass

    store = app.CONTROL
    password = secrets.token_urlsafe(32)
    store.initialize(password, "fixture")
    app.WORKSPACE.write_text('{"key":"demo","locale":{"layers":{}}}\n')
    control_api.RELOAD_DIR.mkdir()
    mailbox_lock = control_api.RELOAD_DIR / ".request.lock"
    mailbox_lock.touch()
    oauth = sql_store.SqlStore(dsn)
    token_a = "mapp_a_" + secrets.token_urlsafe(32)
    client_secret = secrets.token_urlsafe(32)
    scopes = ("mcp:connect", "inspect", "reload", "apply")
    with ExitStack() as stack:
        sql_store.CONTROL_CONNECTIONS = cc.ControlConnections()
        stack.callback(sql_store.CONTROL_CONNECTIONS.close)
        stack.callback(cc.CONTROL_CONNECTIONS.close)
        oauth.add_client(Client("fixture", "fixture", (), scopes))
        oauth.add_client(Client("broker", "broker", (), (), token_endpoint_auth_method="client_secret_basic",
                                client_secret=client_secret, capabilities=("introspect", "exchange", "redeem")))
        oauth.save_grant(Grant("oauth:fixture", "fixture", "operator", scopes))
        oauth.save_token(token_a, Token("", "fixture", " ".join(scopes), "oauth:fixture", int(time.time()),
                                        3600, audience="http://mcp.localhost/mcp"))
        auth_port = stack.enter_context(serving(ControlServer(("127.0.0.1", 0), build_authorization(oauth))))
        config_port = stack.enter_context(serving(ThreadingHTTPServer(("127.0.0.1", 0), Handler)))
        endpoint = f"http://127.0.0.1:{auth_port}"
        app.MCP_TOKENS = app.McpTokenClient(endpoint, "broker", client_secret, resource=app.CONFIG_API_RESOURCE)
        ex = ExchangeClient(broker_endpoint=endpoint, config_api_endpoint=f"http://127.0.0.1:{config_port}",
                            config_api_resource=app.CONFIG_API_RESOURCE, client_id="broker", client_secret=client_secret)

        def exchange(args):
            scope = "apply" if args["operation_id"] == "proposals.apply" else "reload"
            return ex.exchange(subject_token=token_a, scope=scope, **args)

        def approval(args):
            result = store.create_approval(grant_id="oauth:fixture", client_id="fixture", instance="fixture",
                                           operation_id=args["operation_id"], tool="fixture", risk="high",
                                           scopes=["apply" if args["operation_id"] == "proposals.apply" else "reload"],
                                           request_digest=ex.request_digest(**args), packet={})
            assert store.confirm_approval(result["handle"], approved=True, decided_by="fixture")
            receipt = store.claim_receipt(result["handle"])
            assert receipt
            return result["reference"], receipt

        def submit(args, token, receipt, seconds=2):
            conn = http.client.HTTPConnection("127.0.0.1", config_port, timeout=4)
            headers = {"Authorization": "Bearer " + token, "X-Mapp-Timeout-Seconds": str(seconds),
                       "Content-Type": "application/json"}
            if receipt:
                headers[app.APPROVAL_RECEIPT_HEADER] = receipt
            conn.request("POST", args["path"], json.dumps(args["body"]), headers)
            return conn

        def finish(conn):
            try:
                response = conn.getresponse()
                result = json.loads(response.read())
                return response.status, result
            finally:
                conn.close()

        def request(args, token, receipt):
            status, result = finish(submit(args, token, receipt))
            return status, result, reports.get(timeout=2)

        def snapshot():
            def read(name):
                path = control_api.RELOAD_DIR / name
                return path.read_bytes() if path.exists() else None
            return read("expected-workspace"), read("requested"), set(store.operations.iterdir())

        def held_request(args, reference, receipt, module, lock_name, lock, disconnect):
            # Signal at the specific acquisition with the blocker held.
            token = exchange(args)
            entered = threading.Event()
            original = getattr(module, lock_name)

            def observed(*values):
                if not hasattr(lock, "acquire") or values[0] is lock:
                    entered.set()
                return original(*values)

            before = snapshot()
            start = time.monotonic()
            with ExitStack() as block:
                if hasattr(lock, "acquire"):
                    block.enter_context(lock)
                else:
                    descriptor = block.enter_context(lock.open("a"))
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                block.enter_context(patch.object(module, lock_name, observed))
                conn = submit(args, token, receipt, 4 if disconnect else .45)
                assert entered.wait(1), "request did not reach the held lock"
                if disconnect:
                    started_disconnect = time.monotonic()
                    conn.close()
                else:
                    status, body = finish(conn)
                    assert status == 503 and body["retryable"] and not body["indeterminate"], status
                facts = reports.get(timeout=1)
                elapsed = time.monotonic() - (started_disconnect if disconnect else start)
                assert elapsed < (.2 if disconnect else .65), ("waiter overrun", elapsed)
                assert oauth.exchanged_binding(token)["consumed_at"] is not None
                assert facts.token_b == "spent" and facts.receipt == "pending" and not facts.target_started
                if not disconnect:
                    assert body["mutation"] == facts.facts()
                assert store.read_approval_by_reference(reference)["status"] == "approved"
                assert snapshot() == before
            assert snapshot() == before
            name = next((name for name in ("SAVE_RELOAD_LOCK", "PROPOSAL_LOCK", "RELOAD_LOCK")
                         if getattr(module, name, None) is lock), getattr(lock, "name", lock_name))
            print("waiter cleared while held", name, "disconnect" if disconnect else "expiry",
                  round(time.monotonic() - start, 3), flush=True)

        reload = dict(operation_id="xyz.reload", method="POST", path_template="/api/xyz/reload",
                      path="/api/xyz/reload", query="", body={"confirmed": True, "timeout": .1})
        ref, receipt = approval(reload)
        for module, function, lock in ((app, "request_lock", app.SAVE_RELOAD_LOCK),
                                       (control_api, "request_lock", control_api.RELOAD_LOCK),
                                       (control_api, "request_flock", mailbox_lock)):
            for disconnect in (False, True):
                held_request(reload, ref, receipt, module, function, lock, disconnect)

        def acknowledge():
            requested = control_api.RELOAD_DIR / "requested"
            deadline = time.monotonic() + 2
            while not requested.exists():
                assert time.monotonic() < deadline
                time.sleep(.005)
            (control_api.RELOAD_DIR / "workspace-fingerprint").write_bytes(snapshot()[0])
            (control_api.RELOAD_DIR / "applied").write_bytes(requested.read_bytes())
            (control_api.RELOAD_DIR / "healthy").write_text("true")

        tokens = [exchange(reload), exchange(reload)]
        ready = threading.Barrier(2)
        eligible = store.eligible_receipt

        def competing(*args, **kwargs):
            reference = eligible(*args, **kwargs)
            assert reference == ref
            ready.wait(2)
            return reference

        with patch.object(store, "eligible_receipt", competing), ThreadPoolExecutor(3) as workers:
            ack = workers.submit(acknowledge)
            responses = [workers.submit(lambda token: finish(submit(reload, token, receipt)), t) for t in tokens]
            assert sorted(job.result(4)[0] for job in responses) == [200, 403]
            ack.result(3)
        states = [reports.get(timeout=2), reports.get(timeout=2)]
        assert sum(s.target_started for s in states) == 1
        assert store.read_approval_by_reference(ref)["recovery"]["operationId"] == ref[:32]
        assert snapshot()[1] == b"1\n" and len(snapshot()[2]) == 1
        assert request(reload, tokens[0], receipt)[0] in {401, 403}
        assert request(reload, exchange(reload), receipt)[0] == 403
        print("fresh exact exchanges: one consumed receipt, one generation, replay refused")

        ref, receipt = approval(reload)
        token = exchange(reload)
        assert request(reload, token, None)[0] == 403
        assert request({**reload, "body": {"confirmed": True}}, token, receipt)[0] == 403
        wrong_audience = exchange(reload)
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("UPDATE control.oauth_tokens SET audience=%s WHERE token_hash=%s",
                       ("http://mcp.localhost/mcp", sql_store.token_digest(wrong_audience)))
        assert request(reload, wrong_audience, receipt)[0] == 401
        assert oauth.exchanged_binding(wrong_audience)["consumed_at"] is None
        assert store.read_approval_by_reference(ref)["status"] == "approved"
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("UPDATE control.approvals SET decided_at=now()-interval '1 hour' WHERE id_hash=%s", (ref,))
        assert request(reload, exchange(reload), receipt)[0] == 403
        # Hold the approval row past receipt expiry with the operational
        # budget still live. An earlier eligibility read cannot extend it.
        ref, receipt = approval(reload)
        token = exchange(reload)
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("UPDATE control.approvals SET decided_at=clock_timestamp()-interval '299.4 seconds'"
                       " WHERE id_hash=%s", (ref,))
            with db.transaction():
                db.execute("SELECT id_hash FROM control.approvals WHERE id_hash=%s FOR UPDATE", (ref,))
                conn = submit(reload, token, receipt)
                end = time.monotonic() + .4
                while not db.execute("SELECT EXISTS (SELECT 1 FROM pg_stat_activity"
                                     " WHERE datname=current_database() AND wait_event_type='Lock'"
                                     " AND query LIKE 'WITH eligible AS MATERIALIZED%')").fetchone()[0]:
                    assert time.monotonic() < end
                    db.execute("SELECT pg_stat_clear_snapshot()")
                    time.sleep(.005)
                time.sleep(.61)
        assert finish(conn)[0] == 403 and reports.get(timeout=2).receipt == "refused"
        assert store.read_approval_by_reference(ref)["status"] == "approved"

        # Revoke after the private eligibility read, before the serialized spend.
        _, receipt = approval(reload)
        arrived = threading.Event()
        acquire = app.request_lock

        def observe_outer(lock):
            if lock is app.SAVE_RELOAD_LOCK:
                arrived.set()
            return acquire(lock)

        with patch.object(app, "request_lock", observe_outer), app.SAVE_RELOAD_LOCK:
            conn = submit(reload, exchange(reload), receipt)
            assert arrived.wait(1)
            store.revoke_approvals_for_grant("oauth:fixture", "fixture")
        assert finish(conn)[0] == 403 and reports.get(timeout=2).receipt == "refused"
        assert snapshot()[1] == b"1\n"
        stale = {**reload, "body": {**reload["body"], "workspaceFingerprint": "0" * 64}}
        ref, receipt = approval(stale)
        status, _, facts = request(stale, exchange(stale), receipt)
        assert status == 409 and not facts.target_started and facts.receipt == "pending"
        assert store.read_approval_by_reference(ref)["status"] == "approved"
        print("missing, different body, wrong audience, stale fingerprint, expired, revoked and replay refused")

        _, original, revision = app.read_workspace()
        candidate = {**original, "key": "candidate"}
        proposal = app.proposal_create(store, original, revision, candidate, [], [], "fixture")
        target = {"proposalId": proposal["id"], "candidateHash": proposal["candidateHash"], "originalRevision": revision}
        evidence = store.create_operation("proposal.screenshot", "fixture", target)
        artifact_dir = store.root / "artifacts" / "fixture"
        artifact_dir.mkdir(parents=True)
        artifacts = {"afterMap": "fixture/map.png"}
        (artifact_dir / "report.json").write_text(json.dumps({"runId": "fixture", "artifacts": artifacts}))
        (artifact_dir / "map.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aXkAAAAASUVORK5CYII="))
        evidence = store.finish_operation(evidence["id"], status="succeeded",
                                          result={"visual": {"renderPassed": True, "passed": True, "artifacts": artifacts}})
        bundle = app.review_bundle(evidence, store.root / "artifacts", app.ARTIFACT_DOWNLOAD_KEY, "")
        assert bundle["eligible"]
        apply = dict(operation_id="proposals.apply", method="POST", path_template="/api/proposals/{proposalId}/apply",
                     path=f"/api/proposals/{proposal['id']}/apply", query="",
                     body={"approved": True, "acknowledgeIncompletePreview": True, **bundle["binding"]})
        ref, receipt = approval(apply)
        for changed in ({**apply, "body": {**apply["body"], "originalRevision": "stale"}},
                        {**apply, "path": "/api/proposals/other/apply"}):
            assert request(changed, exchange(changed), receipt)[0] == 403
        held_request(apply, ref, receipt, app, "request_lock", app.PROPOSAL_LOCK, False)
        held_request(apply, ref, receipt, app, "request_lock", app.SAVE_RELOAD_LOCK, False)
        held_request(apply, ref, receipt, derived_drafts, "request_flock", store.root / "draft-lifecycle.lock", True)
        assert app.proposal_read(store, proposal["id"])["status"] == "pending"

        def cancel_after_spend(event, **kwargs):
            if event == "approval.consumed":
                cc.current_request().cancel("fixture boundary after database acknowledgement")
            return audit(event, **kwargs)

        audit = store.audit
        with patch.object(store, "audit", cancel_after_spend):
            status, body, facts = request(apply, exchange(apply), receipt)
        assert body["mutation"] == facts.facts() and not body["retryable"] and not body["indeterminate"]
        assert (status, facts.receipt, facts.target_started, facts.retryable) == (503, "consumed", False, False), (
            status, facts.receipt, facts.target_started, facts.retryable)
        recovery = store.read_approval_by_reference(ref)["recovery"]
        assert recovery["operationId"] == ref[:32] and recovery["proposalId"] == proposal["id"]
        assert not (store.operations / (ref[:32] + ".json")).exists()
        assert app.proposal_read(store, proposal["id"])["status"] == "pending"
        assert app.read_workspace()[2] == revision
        print("apply after spend: consumed, target unstarted, recovery available before operation file")

        ref, receipt = approval(apply)
        write_proposal = app.proposal_write

        def cancel_before_applying(control, value, **kwargs):
            assert value["status"] == "applying"
            cc.current_request().cancel("fixture after operation file, before applying")
            return write_proposal(control, value, **kwargs)

        with patch.object(app, "proposal_write", cancel_before_applying):
            status, body, facts = request(apply, exchange(apply), receipt)
        assert status == 503 and body["mutation"] == facts.facts()
        assert facts.receipt == "consumed" and not facts.target_started
        assert store.read_operation(ref[:32])["status"] == "failed"
        assert app.proposal_read(store, proposal["id"])["status"] == "pending"
        assert app.read_workspace()[2] == revision

        ref, receipt = approval(apply)
        def stop_before_ack(candidate):
            raise StopAfterReplace()

        with patch.object(app, "sync_layer_dependency_guard", stop_before_ack):
            conn = submit(apply, exchange(apply), receipt)
            try:
                finish(conn)
            except http.client.RemoteDisconnected:
                pass
            else:
                raise AssertionError("unexpected apply acknowledgement")
        facts = reports.get(timeout=2)
        saved_revision = app.read_workspace()[2]
        assert facts.receipt == "consumed" and facts.target_started and not facts.retryable
        assert facts.published["workspaceRevision"] == saved_revision and saved_revision != revision
        assert app.proposal_read(store, proposal["id"])["status"] == "applying"
        assert store.read_operation(ref[:32])["target"]["candidateHash"] == app.workspace_hash(app.read_workspace()[1])
        store.recover_interrupted_operations()
        assert store.read_operation(ref[:32])["status"] == "indeterminate"
        assert request(apply, exchange(apply), receipt)[0] == 403
        assert app.read_workspace()[2] == saved_revision and snapshot()[1] == b"1\n"
        print("apply after replacement: lost response, committed revision, recoverable proposal, no replay")

        # Non-single-use redemption, a POST, or a CSRF check alone says
        # nothing about the requested target. Observe the normal branches.
        cli = store.create_token("fixture")[0]
        conn = http.client.HTTPConnection("127.0.0.1", config_port, timeout=2)
        conn.request("GET", "/api/operations/" + ref[:32], headers={"Authorization": "Bearer " + cli})
        status, result = finish(conn)
        assert status == 200 and result["operation"]["status"] == "indeterminate"
        assert result["operation"]["target"]["approvalReference"] == ref

        def post(path, body, seconds=2):
            conn = submit({"path": path, "body": body}, cli, None, seconds)
            status, result = finish(conn)
            return status, result, reports.get(timeout=2)

        operations = [{"op": "set", "path": "/key", "value": "saved"}]
        payload = {"operations": operations, "revision": saved_revision}
        for path in ("/api/proposals/check", "/api/mutate"):
            status, _, facts = post(path, payload)
            assert status == 200 and not facts.target_started
        with app.SAVE_LOCK:
            status, _, facts = post("/api/mutate", {**payload, "save": True}, .2)
            assert status == 503 and not facts.target_started
        assert app.read_workspace()[2] == saved_revision

        login = {"password": password}
        status, _, facts = post("/api/auth/login", login)
        assert status == 200 and facts.target_started
        approval_body = {"requestDigest": ex.request_digest(**reload), "operationId": "xyz.reload"}
        status, created, facts = post("/api/approvals", approval_body)
        assert status == 200 and facts.target_started
        handle = {"handle": created["handle"]}
        status, _, facts = post("/api/approvals/claim", handle)
        assert status == 200 and not facts.target_started
        status, _, facts = post("/api/approvals/confirm", {**handle, "accepted": True})
        assert status == 200 and facts.target_started
        status, result, facts = post("/api/approvals/claim", handle)
        assert status == 200 and result["receipt"] and facts.target_started
        status, result, facts = post("/api/approvals/claim", handle)
        assert status == 200 and result["receipt"] is None and not facts.target_started

        # Expiry during the supervisor wait retains the committed revision.
        status, result, facts = post("/api/mutate", {**payload, "save": True}, .35)
        assert status == 503 and facts.target_started and not facts.retryable
        assert facts.published["workspaceRevision"] == app.read_workspace()[2]
        assert facts.published["requestedGeneration"] == 2 and result["indeterminate"]
        print("effect boundaries: login, approval create/confirm/claim, read-only POST, SAVE_LOCK, save and reload wait")

        long_reload = {**reload, "body": {"confirmed": True, "timeout": 30}}
        ref, receipt = approval(long_reload)
        token = exchange(long_reload)
        start = time.monotonic()
        status, body = finish(submit(long_reload, token, receipt, .35))
        facts = reports.get(timeout=1)
        assert time.monotonic() - start < .55
        assert status == 503 and body["indeterminate"] and not body["retryable"]
        assert body["mutation"] == facts.facts()
        assert facts.published == {"requestedGeneration": 3}
        assert store.read_operation(ref[:32])["status"] == "indeterminate"
        assert request(long_reload, exchange(long_reload), receipt)[0] == 403
        assert snapshot()[1] == b"3\n"
        print("direct reload expired after publication: known generation and indeterminate operation retained")

        if check_wire:
            check_wire(app, store, oauth, ex, cli, token_a, client_secret, config_port, auth_port, dsn, root)


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("This fixture requires assertions.")
    with fixture_database(sys.argv[1]) as dsn, tempfile.TemporaryDirectory(prefix="oma114-http-") as tmp:
        run(Path(tmp), dsn)
