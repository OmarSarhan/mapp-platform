#!/usr/bin/env python3
"""Contention and capacity evidence for the ``control`` schema.

The Phase 0 gate asks for the store to be benchmarked "under expected
exchange, approval, replay and cleanup contention" with capacity, failure and
recovery behaviour recorded. This produces those figures rather than adjectives.

It is evidence, not a test: the numbers depend on the machine, so nothing here
asserts a throughput. What it *does* assert are the invariants that must hold
whatever the timings say -- exactly one winner per single-use record, and a
refusal rather than an error when a bound is reached. Those assertions are the
reason this can be run as a check and not merely read.

Usage:

    CONTROL_BENCHMARK_DATABASE_URL=postgresql://... \\
        python scripts/control_plane_benchmark.py [--json]

Use an exclusively disposable database with an empty ``control`` schema.
A repeat run also accepts the current migrated schema with only its ledger
and migration-seeded metadata. The preflight runs before the first migration.
"""

from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures
import datetime as dt
import json
import os
import secrets
import statistics
import sys
import time
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "mcp-auth"))
sys.path.insert(0, str(ROOT / "config-ui"))

import psycopg  # noqa: E402

import control_schema as cs  # noqa: E402
from benchmark_target import RefusedDestructiveRun, guard_scratch_database  # noqa: E402
from control_connections import AdmissionTimeout  # noqa: E402
from models import Client, Grant, PendingAuthorization  # noqa: E402
from sql_store import (  # noqa: E402
    MAX_PENDING_PER_SOURCE,
    PendingLimitReached,
    SqlStore,
)

#: Matches the deployed PostgreSQL role limit, shared across service pools.
DEPLOYED_CONNECTION_LIMIT = 8

#: The benchmark's working set: the schema's delete order minus the credential
#: tables. The order is shared so foreign keys stay satisfied as the ladder
#: grows, but admin_credential and metadata are excluded deliberately --
#: clearing admin_credential would disarm this script's own destructive guard,
#: and the deployment-helper suite shares this scratch database and asserts on
#: what ./bin/mapp init writes there.
#:
#: Taking the shared order wholesale reintroduced exactly that, and the test
#: written to forbid it caught the change.
TABLES = tuple(
    table
    for table in cs.TABLES_IN_DELETE_ORDER
    if table not in ("admin_credential", "metadata")
)


def truncate(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        for table in TABLES:
            connection.execute(f"DELETE FROM control.{table}")


def seed(store: SqlStore) -> None:
    store.add_client(
        Client(
            client_id="bench-client",
            name="Benchmark",
            redirect_uris=("http://127.0.0.1:9/cb",),
            scopes=("apply",),
            token_endpoint_auth_method="none",
        )
    )
    store.save_grant(
        Grant(
            grant_id="oauth:bench",
            client_id="bench-client",
            subject="operator",
            scopes=("apply",),
        )
    )


def mint_exchanged(store: SqlStore, raw: str, digest: str) -> None:
    issued = dt.datetime.now(dt.timezone.utc)
    store.save_exchanged_token(
        raw,
        client_id="bench-client",
        actor_client_id="bench-client",
        subject="oauth:bench",
        scope="apply",
        audience="http://config.localhost/api",
        issued_at=issued,
        expires_at=issued + dt.timedelta(seconds=600),
        operation_id="proposals.apply",
        request_digest=digest,
        single_use=True,
    )


class Attempt(NamedTuple):
    outcome: str
    ms: float
    value: object


def measure(callable_, *args, spend=False) -> Attempt:
    start = time.perf_counter()
    try:
        value = callable_(*args)
        outcome = "replay_loss" if spend and value is None else "success"
    except AdmissionTimeout:
        outcome, value = "refused", None
    except Exception as exc:
        # Diagnostics may include SQL parameters or connection details.
        outcome, value = "error", type(exc).__name__
    return Attempt(outcome, (time.perf_counter() - start) * 1000.0, value)


def counts(attempts):
    outcomes = Counter(attempt.outcome for attempt in attempts)
    totals = {key: outcomes[key] for key in ("success", "replay_loss", "refused", "error")}
    totals["attempted"] = len(attempts)
    totals["admitted"] = totals["success"] + totals["replay_loss"]
    if totals["error"]:
        types = sorted({attempt.value for attempt in attempts if attempt.outcome == "error"})
        raise AssertionError(f"unexpected benchmark errors {types}, counts {totals}")
    return totals


def replay_contention(dsn: str, *, presenters: int, trials: int) -> dict:
    """One token, many simultaneous presentations. Exactly one may win.

    This is the property single use exists for, measured under real
    concurrency across separate connections rather than threads sharing one.
    """
    store = SqlStore(dsn)
    digest = "mapp-jcs-v1:" + "a" * 64
    winners, elapsed, attempts = [], [], []
    for trial in range(trials):
        truncate(dsn)
        seed(store)
        raw = f"mapp_b_replay_{trial}"
        mint_exchanged(store, raw, digest)
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(presenters) as pool:
            results = list(
                pool.map(
                    lambda _: measure(
                        store.consume_exchanged_token, raw, "proposals.apply", digest, spend=True
                    ),
                    range(presenters),
                )
            )
        elapsed.append((time.perf_counter() - start) * 1000.0)
        winners.append(counts(results)["success"])
        attempts.extend(results)
        replay = store.consume_exchanged_token(raw, "proposals.apply", digest)
        assert replay is None, f"spent token won again after trial {trial}"
    assert set(winners) == {1}, f"expected exactly one winner per trial, got {winners}"
    return {
        "scenario": "replay",
        "presenters": presenters,
        "trials": trials,
        "winners_per_trial": sorted(set(winners)),
        **counts(attempts),
        "post_burst_replay_losses": trials,
        "median_ms": round(statistics.median(elapsed), 1),
        "max_ms": round(max(elapsed), 1),
        "invariant": "exactly one winner per trial",
    }


def exchange_throughput(dsn: str, *, workers: int, tokens: int) -> dict:
    """Distinct tokens minted and spent concurrently: the steady-state path."""
    store = SqlStore(dsn)
    digest = "mapp-jcs-v1:" + "b" * 64
    truncate(dsn)
    seed(store)
    for index in range(tokens):
        mint_exchanged(store, f"mapp_b_flow_{index}", digest)
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        attempts = list(pool.map(
            lambda index: measure(
                store.consume_exchanged_token,
                f"mapp_b_flow_{index}",
                "proposals.apply",
                digest,
                spend=True,
            ),
            range(tokens),
        ))
    wall = time.perf_counter() - start
    totals = counts(attempts)
    assert totals["success"] == tokens, f"every distinct token must spend once: {totals}"
    latencies = [attempt.ms for attempt in attempts]
    return {
        "scenario": "exchange-throughput",
        "workers": workers,
        "tokens": tokens,
        "wall_seconds": round(wall, 2),
        "spends_per_second": round(tokens / wall, 1),
        "median_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1),
        **totals,
        "failures": tokens - totals["success"],
        "invariant": "every distinct token spends exactly once",
    }


def approval_admission(dsn: str, *, source: str = "203.0.113.9") -> dict:
    """The per-source pending cap: it must refuse, not error, and not lock out.

    P21 exists because a global cap alone was worse than none -- parking a
    record is unauthenticated and free, so one caller could fill the shared
    table and deny every other user. The bound being per source is what makes
    a flood deny only the flooder, so that is what is measured: the flooder
    hits its ceiling and a second source is unaffected.
    """
    store = SqlStore(dsn)
    truncate(dsn)
    seed(store)
    accepted, refused = 0, 0
    for index in range(MAX_PENDING_PER_SOURCE + 10):
        try:
            store.save_pending(
                PendingAuthorization(
                    request_id=f"flood-{index}",
                    query="response_type=code",
                    client_id="bench-client",
                    redirect_uri="http://127.0.0.1:9/cb",
                    scopes=("apply",),
                    csrf="x" * 32,
                    source=source,
                )
            )
            accepted += 1
        except PendingLimitReached:
            refused += 1
    # A different source must still be admitted: that is the whole point.
    store.save_pending(
        PendingAuthorization(
            request_id="other-source",
            query="response_type=code",
            client_id="bench-client",
            redirect_uri="http://127.0.0.1:9/cb",
            scopes=("apply",),
            csrf="y" * 32,
            source="198.51.100.4",
        )
    )
    assert accepted == MAX_PENDING_PER_SOURCE, (
        f"expected {MAX_PENDING_PER_SOURCE} admitted, got {accepted}"
    )
    assert refused == 10, f"expected 10 refusals, got {refused}"
    return {
        "scenario": "approval-admission",
        "per_source_cap": MAX_PENDING_PER_SOURCE,
        "attempted": accepted + refused,
        "admitted": accepted,
        "refused": refused,
        "success": accepted,
        "error": 0,
        "other_source_still_admitted": True,
        "invariant": "a flood denies only the flooder, and refuses rather than errors",
    }


def cleanup_under_load(dsn: str, *, writers: int, sweeps: int) -> dict:
    """Expiry cleanup while records are being written.

    save_pending sweeps opportunistically, so cleanup and admission already
    contend in normal operation. What matters is that neither deadlocks and
    that a sweep does not remove a live record.
    """
    store = SqlStore(dsn)
    truncate(dsn)
    seed(store)
    issued = dt.datetime.now(dt.timezone.utc)
    # Half already expired, so a sweep has real work to do.
    with psycopg.connect(dsn, autocommit=True) as connection:
        for index in range(200):
            expires = issued + dt.timedelta(seconds=-1 if index % 2 else 600)
            connection.execute(
                "INSERT INTO control.oauth_pending_authorizations"
                "(request_id, query, client_id, redirect_uri, scopes, csrf,"
                " source, expires_at)"
                " VALUES(%s,'q','bench-client','http://127.0.0.1:9/cb',"
                # A source the writers below never use: these are inserted
                # past save_pending, so 100 of them are live, and the
                # per-source cap counts live records however they arrived.
                " '{apply}','c','203.0.113.200',%s)",
                (f"sweep-{index}", expires),
            )

    def pending(request_id, source):
        return PendingAuthorization(
            request_id=request_id,
            query="response_type=code",
            client_id="bench-client",
            redirect_uri="http://127.0.0.1:9/cb",
            scopes=("apply",),
            csrf="z" * 32,
            source=source,
        )

    with concurrent.futures.ThreadPoolExecutor(writers + sweeps) as pool:
        sweep_jobs = [pool.submit(measure, store.sweep_expired) for _ in range(sweeps)]
        write_jobs = {}
        for index in range(writers):
            record = pending(f"live-{index}", f"198.51.100.{index}")
            write_jobs[record.request_id] = pool.submit(measure, store.save_pending, record)
        sweep_results = [job.result() for job in sweep_jobs]
        writes = {key: job.result() for key, job in write_jobs.items()}
    attempts = [*sweep_results, *writes.values()]
    totals = counts(attempts)
    admitted = {key for key, attempt in writes.items() if attempt.outcome == "success"}
    refused = writes.keys() - admitted
    expected = {f"sweep-{n}" for n in range(0, 200, 2)} | admitted
    with psycopg.connect(dsn, autocommit=True) as connection:
        query = "SELECT request_id, expires_at > now() FROM control.oauth_pending_authorizations"
        rows = connection.execute(query).fetchall()
        assert {key for key, live in rows if live} == expected, "unexpected live IDs after burst"
        assert refused.isdisjoint(key for key, _ in rows), "a refused write persisted"
        follow_up = store.sweep_expired()
        survivors = dict(connection.execute(query).fetchall())
        expired_remaining = sum(not live for live in survivors.values())
        assert survivors.keys() == expected, "follow-up sweep left unexpected IDs"
        assert expired_remaining == 0, "expired rows remained after follow-up sweep"
        store.save_pending(pending("fresh-write", "192.0.2.1"))
        assert store.query_pending("fresh-write") is not None, "fresh write missing after saturation"
    return {
        "scenario": "cleanup-under-load",
        "concurrent_writers": writers,
        "concurrent_sweeps": sweeps,
        "expired_seeded": 100,
        **totals,
        "writer_counts": counts(writes.values()),
        "sweep_counts": counts(sweep_results),
        "admitted_writer_ids": sorted(admitted),
        "refused_writer_ids": sorted(refused),
        "removed_total": sum(r.value for r in sweep_results if r.outcome == "success"),
        "follow_up_removed": follow_up,
        "live_survivors": len(survivors),
        "expired_remaining": expired_remaining,
        "fresh_write_recovered": True,
        "max_ms": round(max(attempt.ms for attempt in attempts), 1),
        "invariant": "no deadlock, and no live record removed",
    }


def connection_ceiling(dsn: str, *, limit: int = DEPLOYED_CONNECTION_LIMIT) -> dict:
    """Measure PostgreSQL's role ceiling independently of service pool admission."""
    role = "bench_capped_" + secrets.token_hex(12)
    ident = psycopg.sql.Identifier(role)
    password = secrets.token_urlsafe(32)
    capped = psycopg.conninfo.make_conninfo(dsn, user=role, password=password)
    held = []
    with psycopg.connect(dsn, autocommit=True) as admin:
        verifier = admin.pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256")
        admin.execute(psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT {}").format(
            ident, psycopg.sql.Literal(verifier.decode()), psycopg.sql.Literal(limit)
        ))
        # A failed CREATE (including a name collision) never enters cleanup.
        try:
            for _ in range(limit):
                held.append(psycopg.connect(capped))
            opened = len(held)
            try:
                held.append(psycopg.connect(capped))
            except psycopg.OperationalError as exc:
                # libpq startup errors can lack a SQLSTATE. Require the
                # role-specific server diagnostic and omit connection details.
                refusal = "too many connections for role"
                if f'{refusal} "{role}"' not in str(exc):
                    raise
            else:
                raise AssertionError("connection above the role limit was admitted")
            held.pop().close()
            with psycopg.connect(capped):
                pass
        finally:
            for connection in held:
                connection.close()
            admin.execute(psycopg.sql.SQL("DROP ROLE {}").format(ident))
    return {
        "scenario": "connection-ceiling",
        "role_limit": limit,
        "connections_opened": opened,
        "attempted": opened + 1,
        "admitted": opened,
        "refused": 1,
        "success": opened,
        "error": 0,
        "refusal": refusal,
        "recovers_after_release": True,
        "invariant": "refuses at the cap, and recovers when one is released",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument(
        "--force-destructive",
        action="store_true",
        help="run even though the target holds an administrator credential",
    )
    parser.add_argument("--presenters", type=int, default=16)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--tokens", type=int, default=200)
    arguments = parser.parse_args()

    dsn = os.environ.get("CONTROL_BENCHMARK_DATABASE_URL", "")
    if not dsn:
        print(
            "Set CONTROL_BENCHMARK_DATABASE_URL to a scratch database whose"
            " control schema may be emptied.",
            file=sys.stderr,
        )
        return 2

    try:
        guard_scratch_database(dsn, force=arguments.force_destructive)
    except RefusedDestructiveRun as exc:
        print(str(exc), file=sys.stderr)
        return 2

    with cs.connect(dsn) as connection:
        cs.migrate(connection)

    results = [
        replay_contention(dsn, presenters=arguments.presenters, trials=arguments.trials),
        exchange_throughput(dsn, workers=arguments.workers, tokens=arguments.tokens),
        approval_admission(dsn),
        cleanup_under_load(dsn, writers=16, sweeps=4),
        connection_ceiling(dsn),
    ]
    truncate(dsn)

    if arguments.json:
        print(json.dumps(results, indent=2))
        return 0
    for result in results:
        print(f"\n## {result.pop('scenario')}")
        invariant = result.pop("invariant")
        for key, value in result.items():
            print(f"  {key:32} {value}")
        print(f"  {'INVARIANT HELD':32} {invariant}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
