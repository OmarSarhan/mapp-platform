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

The database must have an empty ``control`` schema that may be truncated. The
schema is created by a superuser in the packaged image, never by its owning
role, so this assumes it already exists -- the same assumption the test
fixtures make.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-auth"))
sys.path.insert(0, str(ROOT / "config-ui"))

import psycopg  # noqa: E402

import control_schema as cs  # noqa: E402
from models import Client, Grant, PendingAuthorization  # noqa: E402
from sql_store import (  # noqa: E402
    MAX_PENDING_PER_SOURCE,
    PendingLimitReached,
    SqlStore,
)

#: Matches the deployed limit in docker/postgis/init/10-roles.sh. Neither
#: consumer pools, so this is a ceiling on concurrent control-plane operations
#: rather than headroom above two pools -- which is exactly why saturating it
#: is worth measuring rather than assuming.
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


class RefusedDestructiveRun(RuntimeError):
    """The target looks like a real platform, not a scratch database."""


def guard_scratch_database(dsn: str, *, force: bool = False) -> None:
    """Refuse to run against an initialized platform unless forced.

    Everything below deletes every row from six control tables and creates a
    LOGIN role with a known password. Pointed at a live database that destroys
    every grant, token and OAuth client the platform holds -- and the DSN comes
    from an environment variable, which is exactly how that mistake gets made.

    An initialized platform has an admin_credential row; a scratch database
    does not. That is the cheapest reliable signal, and it is the one thing a
    benchmark must not be casual about.
    """
    if force:
        return
    with psycopg.connect(dsn, autocommit=True) as connection:
        initialized = connection.execute(
            "SELECT count(*) FROM control.admin_credential"
        ).fetchone()[0]
    if initialized:
        raise RefusedDestructiveRun(
            "The target database holds an administrator credential, so it is an"
            " initialized platform rather than a scratch database. This"
            " benchmark deletes every row from six control tables. Point"
            " CONTROL_BENCHMARK_DATABASE_URL at a scratch database, or pass"
            " --force-destructive if you are certain."
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


def timed(callable_, *args):
    start = time.perf_counter()
    try:
        result = callable_(*args)
        error = None
    except Exception as exc:  # noqa: BLE001 - the failure mode is the measurement
        result, error = None, f"{type(exc).__name__}: {exc}"
    return (time.perf_counter() - start) * 1000.0, result, error


def replay_contention(dsn: str, *, presenters: int, trials: int) -> dict:
    """One token, many simultaneous presentations. Exactly one may win.

    This is the property single use exists for, measured under real
    concurrency across separate connections rather than threads sharing one.
    """
    store = SqlStore(dsn)
    digest = "mapp-jcs-v1:" + "a" * 64
    winners, elapsed = [], []
    for trial in range(trials):
        truncate(dsn)
        seed(store)
        raw = f"mapp_b_replay_{trial}"
        mint_exchanged(store, raw, digest)
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(presenters) as pool:
            results = list(
                pool.map(
                    lambda _: store.consume_exchanged_token(
                        raw, "proposals.apply", digest
                    ),
                    range(presenters),
                )
            )
        elapsed.append((time.perf_counter() - start) * 1000.0)
        winners.append(sum(1 for item in results if item is not None))
    assert set(winners) == {1}, f"expected exactly one winner per trial, got {winners}"
    return {
        "scenario": "replay",
        "presenters": presenters,
        "trials": trials,
        "winners_per_trial": sorted(set(winners)),
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
    latencies, failures = [], []
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for ms, result, error in pool.map(
            lambda index: timed(
                store.consume_exchanged_token,
                f"mapp_b_flow_{index}",
                "proposals.apply",
                digest,
            ),
            range(tokens),
        ):
            latencies.append(ms)
            if error is not None:
                failures.append(error)
            elif result is None:
                failures.append("unspendable")
    wall = time.perf_counter() - start
    assert not failures, f"every distinct token must spend once: {failures[:3]}"
    return {
        "scenario": "exchange-throughput",
        "workers": workers,
        "tokens": tokens,
        "wall_seconds": round(wall, 2),
        "spends_per_second": round(tokens / wall, 1),
        "median_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1),
        "failures": len(failures),
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
        "admitted": accepted,
        "refused": refused,
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
    errors, removed = [], []

    def sweep(_):
        ms, result, error = timed(store.sweep_expired)
        if error:
            errors.append(error)
        else:
            removed.append(result)
        return ms

    def write(index):
        ms, _, error = timed(
            store.save_pending,
            PendingAuthorization(
                request_id=f"live-{index}",
                query="response_type=code",
                client_id="bench-client",
                redirect_uri="http://127.0.0.1:9/cb",
                scopes=("apply",),
                csrf="z" * 32,
                source=f"198.51.100.{index}",
            ),
        )
        if error:
            errors.append(error)
        return ms

    with concurrent.futures.ThreadPoolExecutor(writers + sweeps) as pool:
        jobs = [pool.submit(sweep, n) for n in range(sweeps)]
        jobs += [pool.submit(write, n) for n in range(writers)]
        latencies = [job.result() for job in jobs]
    with psycopg.connect(dsn, autocommit=True) as connection:
        # A plain psycopg connection yields tuples; control_schema.connect is
        # the one that installs a dict row factory.
        survivors = connection.execute(
            "SELECT count(*) FROM control.oauth_pending_authorizations"
            " WHERE expires_at > now()"
        ).fetchone()[0]
    assert not errors, f"cleanup and admission must not error: {errors[:3]}"
    assert survivors >= 100 + writers, (
        f"a sweep removed live records: {survivors} survivors"
    )
    return {
        "scenario": "cleanup-under-load",
        "concurrent_writers": writers,
        "concurrent_sweeps": sweeps,
        "expired_seeded": 100,
        "removed_total": sum(removed),
        "live_survivors": survivors,
        "max_ms": round(max(latencies), 1),
        "invariant": "no deadlock, and no live record removed",
    }


def connection_ceiling(dsn: str, *, limit: int = DEPLOYED_CONNECTION_LIMIT) -> dict:
    """What happens at and above the role's CONNECTION LIMIT.

    The deployed role is capped at 8 and neither consumer pools, so this is a
    ceiling on concurrent operations. The audit that found the budget was
    justified by pools that do not exist left the question of what the ninth
    caller sees; this answers it. A connection-per-operation store turns a
    burst into refusals, not queuing, and the failure is PostgreSQL's rather
    than the application's -- so it is worth knowing exactly what it says.
    """
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        if admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'bench_capped'"
        ).fetchone():
            admin.execute(
                "REVOKE ALL ON ALL TABLES IN SCHEMA control FROM bench_capped"
            )
            admin.execute("REVOKE ALL ON SCHEMA control FROM bench_capped")
            admin.execute("DROP ROLE bench_capped")
        admin.execute(
            f"CREATE ROLE bench_capped LOGIN PASSWORD 'capped'"
            f" CONNECTION LIMIT {limit}"
        )
        admin.execute("GRANT USAGE ON SCHEMA control TO bench_capped")
        admin.execute(
            "GRANT SELECT ON ALL TABLES IN SCHEMA control TO bench_capped"
        )
        parsed = psycopg.conninfo.conninfo_to_dict(dsn)
        capped = psycopg.conninfo.make_conninfo(
            **{**parsed, "user": "bench_capped", "password": "capped"}
        )
        held, refusal = [], None
        try:
            for _ in range(limit + 4):
                held.append(psycopg.connect(capped))
        except psycopg.OperationalError as exc:
            refusal = str(exc).strip().splitlines()[0]
        finally:
            opened = len(held)
            for connection in held:
                connection.close()
        # And the ceiling recovers the moment a connection is returned.
        recovered = False
        try:
            probe = psycopg.connect(capped)
            probe.close()
            recovered = True
        except psycopg.OperationalError:
            pass
    finally:
        # Grants must go before the role: PostgreSQL refuses to drop a role
        # that still owns privileges, and leaving one behind would make a
        # second run fail on CREATE ROLE instead of measuring anything.
        admin.execute(
            "REVOKE ALL ON ALL TABLES IN SCHEMA control FROM bench_capped"
        )
        admin.execute("REVOKE ALL ON SCHEMA control FROM bench_capped")
        admin.execute("DROP ROLE IF EXISTS bench_capped")
        admin.close()
    assert opened == limit, f"expected {limit} connections, opened {opened}"
    assert refusal is not None, "the ceiling must refuse rather than queue"
    assert recovered, "the ceiling must recover when a connection is released"
    return {
        "scenario": "connection-ceiling",
        "role_limit": limit,
        "connections_opened": opened,
        "refusal": refusal,
        "recovers_after_release": recovered,
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

    connection = cs.connect(dsn)
    try:
        cs.migrate(connection)
    finally:
        connection.close()

    try:
        guard_scratch_database(dsn, force=arguments.force_destructive)
    except RefusedDestructiveRun as exc:
        print(str(exc), file=sys.stderr)
        return 2

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
