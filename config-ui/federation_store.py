"""Durable alias registry for federation (architecture waypoint decision #3).

Wires the previously-unwired `federation_schema`/`federation_capability`
validation and detection logic into a real, queryable Postgres store. The
registry lives in a new `federation` schema inside the *existing* derived
database rather than a separate dedicated federation database — the doc's
migration into a standalone federation database (`MAPP_DATABASE_MODE`
`federated`) is a distinct, later piece of work, not required to prove FDW
mechanics end to end.

Deliberately out of scope here (see the accompanying plan): the doc's
write-only secret-submission/verify-not-read endpoints (we resolve
`connectionRef` through the existing `DBS_<ALIAS>` convention instead),
alias-count ceilings, registration TTL, and retire/reclaim. Also
deliberately out of scope: forcing derived layers through an integration
view before they may read a foreign schema. Instead this module answers a
narrower, explicitly requested question — which derived layers currently
read from a given alias, so removing it is an informed choice rather than a
silent break.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from federation_capability import (
    detect_capability,
    extension_versions,
    verify_remote_state,
)
from federation_schema import (
    FederationSchemaError,
    enforce_tls_policy,
    validate_alias,
    validate_registration,
)

SCHEMA = "federation"

# Must match derived_layers.SCHEMA. Hardcoded rather than imported to avoid
# pulling in the whole derived_layers module for one stable constant.
DERIVED_LAYERS_SCHEMA = "derived_layers"


class FederationAliasStore:
    def __init__(self, connection_string: str, reader_role: str):
        if not connection_string:
            raise FederationSchemaError(
                "Federation alias registry is not configured.",
                code="federation.not_configured",
            )
        self.connection_string = connection_string
        self.reader_role = reader_role
        self._initialization_lock = threading.Lock()
        self._initialized = False

    def _connect(self):
        connection = psycopg.connect(
            self.connection_string,
            autocommit=False,
            row_factory=dict_row,
        )
        try:
            with connection.cursor() as cur:
                cur.execute("SET SESSION search_path = pg_catalog, public")
            if self._initialized:
                return connection
            with self._initialization_lock:
                if not self._initialized:
                    with connection.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (f"{SCHEMA}:schema",),
                        )
                        self._initialize(cur)
                    connection.commit()
                    self._initialized = True
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _initialize(cur) -> None:
        # The federation schema itself is provisioned by docker/postgis's
        # role-setup scripts (owned by the derived-owner role), the same way
        # derived_layers is — not created lazily here. The derived-owner
        # role has no CREATE privilege on the database itself to do so.
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._aliases (
              alias text PRIMARY KEY,
              display_name text NOT NULL,
              kind text NOT NULL CHECK (kind = 'postgresql'),
              connection_ref text NOT NULL,
              allowed_relations text[] NOT NULL,
              status text NOT NULL
                CHECK (status IN ('pending', 'active', 'unavailable', 'retired')),
              freshness_strategy text NOT NULL
                CHECK (freshness_strategy IN (
                  'manual', 'maximumAge', 'timestampColumn', 'versionRelation'
                )),
              data_handling_classification text NOT NULL,
              registered_by text NOT NULL,
              registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              last_observation jsonb,
              last_observed_connection_identity text,
              tls_policy text NOT NULL DEFAULT 'require'
                CHECK (tls_policy IN ('require', 'verify-ca', 'verify-full')),
              provisioned_at timestamptz,
              approved_by text,
              approved_at timestamptz,
              physical_identity text,
              observed_at timestamptz,
              row_level_security_acknowledged boolean NOT NULL DEFAULT false,
              accepted_schema_fingerprint text,
              accepted_physical_identity text
            )
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "last_observed_connection_identity text"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS approved_by text"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "approved_at timestamptz"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "physical_identity text"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "observed_at timestamptz"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "row_level_security_acknowledged boolean NOT NULL DEFAULT false"
        ).format(sql.Identifier(SCHEMA)))
        # NULL until a successful provision() explicitly accepts a live
        # schema_fingerprint (see provision()'s docstring) — deliberately
        # not backfilled from last_observation, which reflects whatever
        # the most recent Observe merely *saw*, not what a human actually
        # reviewed and accepted.
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "accepted_schema_fingerprint text"
        ).format(sql.Identifier(SCHEMA)))
        # The durable counterpart to physical_identity, for the same reason
        # accepted_schema_fingerprint is the durable counterpart to
        # last_observation's fingerprint: physical_identity records whatever
        # the most recent Observe merely *saw*, so comparing a live probe
        # against it only ever catches a replacement that happened between
        # Observe and Provision — never one that was already observed. This
        # column records the identity a successful provision() actually
        # accepted, so a source database replaced and then re-observed is
        # still caught (docs/federation-architecture-waypoint.md: "The same
        # table name at a new physical database is not the same source
        # unless an operator performs an explicit, evidenced rebind").
        # Deliberately not backfilled from physical_identity — that would
        # retroactively "accept" whatever is there now, which is exactly the
        # replacement this exists to catch.
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "accepted_physical_identity text"
        ).format(sql.Identifier(SCHEMA)))
        # DEFAULT backfills any alias registered before tlsPolicy was
        # persisted with the weakest of the three valid policies — the
        # conservative choice: it neither claims a stronger guarantee than
        # was ever actually reviewed, nor breaks a currently-working
        # connectionRef that only ever supplied sslmode=require.
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "tls_policy text NOT NULL DEFAULT 'require'"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._aliases FROM PUBLIC"
        ).format(sql.Identifier(SCHEMA)))
        # Every subsequent Observe replaces _aliases.last_observation, so a
        # network outage, credential rejection, or schema drift permanently
        # destroyed the preceding evidence — nothing could explain drift or
        # an outage after the fact. This is the append-only counterpart:
        # every observation ever taken, kept regardless of whether it went
        # on to win record_observation()'s latest-observation race.
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._observations (
              id bigserial PRIMARY KEY,
              alias text NOT NULL REFERENCES {}._aliases (alias),
              observation jsonb NOT NULL,
              connection_identity text,
              physical_identity text,
              observed_at timestamptz NOT NULL,
              recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
        """).format(sql.Identifier(SCHEMA), sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "CREATE INDEX IF NOT EXISTS _observations_alias_observed_at_idx "
            "ON {}._observations (alias, observed_at DESC)"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._observations FROM PUBLIC"
        ).format(sql.Identifier(SCHEMA)))

    _SELECT_COLUMNS = sql.SQL("""
        alias, display_name AS "displayName", kind,
        connection_ref AS "connectionRef",
        allowed_relations AS "allowedRelations", status,
        freshness_strategy AS "freshnessStrategy",
        data_handling_classification AS "dataHandlingClassification",
        registered_by AS "registeredBy",
        registered_at AS "registeredAt",
        last_observation AS "lastObservation",
        tls_policy AS "tlsPolicy",
        provisioned_at AS "provisionedAt",
        approved_by AS "approvedBy",
        approved_at AS "approvedAt",
        row_level_security_acknowledged AS "rowLevelSecurityAcknowledged"
    """)

    def register(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        record = validate_registration(payload)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT 1 FROM {}._aliases WHERE alias = %s").format(
                    sql.Identifier(SCHEMA)
                ),
                (record["alias"],),
            )
            if cur.fetchone():
                raise FileExistsError(record["alias"])
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}._aliases
                      (alias, display_name, kind, connection_ref,
                       allowed_relations, status, freshness_strategy,
                       data_handling_classification, registered_by,
                       tls_policy)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """).format(sql.Identifier(SCHEMA)),
                (
                    record["alias"],
                    record["displayName"],
                    record["kind"],
                    record["connectionRef"],
                    list(record["allowedRelations"]),
                    record["status"],
                    record["freshnessStrategy"],
                    record["dataHandlingClassification"],
                    actor,
                    record["tlsPolicy"],
                ),
            )
        return self.get(record["alias"])

    def get(self, alias: str) -> dict[str, Any]:
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            return self._get_with_cursor(cur, alias)

    def _get_with_cursor(self, cur, alias: str) -> dict[str, Any]:
        """Read an alias through an already-open cursor. `alias` must already
        be validated.

        observe() and provision() return this rather than calling get(), so
        the row they hand back is the one their own transaction produced.
        get() opens a separate connection, which — because it can only see
        committed data — would either miss the caller's own uncommitted work
        or, once committed, race a concurrent writer and return somebody
        else's result. app.py records the returned connectivity in the
        caller's audit event, so returning another request's observation
        would misattribute the evidence."""
        cur.execute(
            sql.SQL("SELECT {} FROM {}._aliases WHERE alias = %s").format(
                self._SELECT_COLUMNS, sql.Identifier(SCHEMA)
            ),
            (alias,),
        )
        item = cur.fetchone()
        if not item:
            raise FileNotFoundError(alias)
        return item

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT {} FROM {}._aliases ORDER BY alias").format(
                    self._SELECT_COLUMNS, sql.Identifier(SCHEMA)
                )
            )
            return list(cur.fetchall())

    def record_observation(
        self,
        alias: str,
        observation: dict[str, Any],
        connection_url: str,
        physical_identity: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Persist an already-validated observation (see
        federation_capability.detect_capability, which computes both
        `observation` and `physical_identity` from the same connection's
        snapshot) — `physical_identity` is None when connectivity wasn't
        "reachable", since it can't be fetched from a source that couldn't
        be reached. `observed_at` marks the moment *this* Observe's
        REPEATABLE READ snapshot was actually fixed on the *remote* (via its
        own clock_timestamp(), captured by the caller as the first query
        inside that snapshot) when reachable, or the moment the connection
        attempt was given up as failed, on this *local* process's clock,
        when not — two different clock domains. Two overlapping Observe
        calls for the same alias can finish and try to write in either
        order; without comparing something, whichever write simply
        *commits* last would win even if its own probe's snapshot was the
        older, now-superseded one. The WHERE clause below compares
        observed_at only between two observations of the *same*
        reachability (both remote-clock, or both local-clock — a single,
        internally consistent domain either way) and makes a same-domain
        stale write a no-op: Postgres serializes concurrent UPDATEs to the
        same row, and each one re-checks this condition against whatever
        the other just committed. When the incoming observation's
        reachability *differs* from what's currently stored, there is no
        shared clock to compare on — comparing a remote timestamp against a
        local one directly would let ordinary clock drift between the two
        hosts discard a genuinely later connectivity-state change (e.g. a
        real, more-recent outage silently losing to an earlier success
        whose remote clock merely happened to run ahead) — so a
        reachability *transition* is instead accepted unconditionally: it
        is new information last_observation doesn't reflect at all, so
        there is no coherent basis to call it "stale" against a different
        clock domain's timestamp.

        `observation["schema"]` as computed by detect_capability() only
        reflects relation existence/selectability, not whether the column
        list or view definition changed since it was last explicitly
        accepted — that comparison is added here as a separate
        `acceptedSchemaCurrent` boolean, deliberately NOT folded into
        `schema` itself. An earlier version of this fix did overwrite
        `schema` to "changed" on a fingerprint mismatch, mirroring how
        detect_capability() already uses "changed" for a missing/
        unselectable relation — but provision() has its own stored-
        evidence fast-fail, `last_observation.get("schema") != "current"`,
        that exists to reject provisioning against stale evidence *before*
        a live round-trip. That check has no acknowledgement escape by
        design (a genuinely missing relation cannot be reconciled by
        acknowledging anything — Observe must run again). Overwriting
        `schema` made a fingerprint drift indistinguishable from a missing
        relation to that check, so it fired first on every drift and
        permanently blocked the very acknowledge_schema_change path this
        module exists to support — caught live against the real FDW rig,
        not in a mock. Keeping `schema` untouched and reporting the
        fingerprint comparison through its own field avoids that
        collision entirely: `schema` keeps meaning exactly what
        detect_capability() computed, and `acceptedSchemaCurrent` (true
        when nothing has been accepted yet or the live fingerprint still
        matches what was) is purely for visibility — provision()'s own,
        separately-computed schema_fingerprint_changed gate (comparing
        the same accepted_schema_fingerprint against a fresh live probe)
        remains the sole authority on whether provisioning may proceed.
        This never writes accepted_schema_fingerprint itself, so it
        cannot affect that gate either way — only what Observe reports.

        Callable directly (used here, and by any caller that already has
        its own observation evidence), but see observe() below for the
        preferred entry point when you also need to run the remote probe
        yourself: it serializes the whole probe-and-persist cycle per
        alias, closing a race this method's own comparisons cannot (see
        observe()'s docstring)."""
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            self._persist_observation(
                cur, alias, observation, connection_url, physical_identity,
                observed_at,
            )
            return self._get_with_cursor(cur, alias)

    def observe(
        self,
        alias: str,
        connection_url: str,
        *,
        allowed_relations: tuple[str, ...],
        tls_policy: str,
        version_relation: str | None = None,
    ) -> dict[str, Any]:
        """The full Observe cycle — remote probe (detect_capability) then
        persisted write (record_observation()'s own logic, reused via
        _persist_observation) — serialized per alias by a held advisory
        lock spanning both, so two concurrent Observe calls for the same
        alias can never interleave.

        This closes a race record_observation()'s own comparisons cannot:
        a reachability *transition* is accepted unconditionally there (see
        its docstring — there is no shared clock between a reachable
        probe's remote-clock timestamp and an unreachable probe's local-
        clock one), so without serialization, an older probe that stalls
        after taking its snapshot could still commit its result *after* a
        newer, more-current probe's result already committed, silently
        overwriting it with stale evidence — or the reverse, an unrelated
        newer outage overwriting a fresher recovery. No single comparison
        rule can fix this after the fact, because the two results being
        compared were never ordered relative to each other in the first
        place. Preventing the interleaving outright removes the ambiguity:
        holding this lock across the remote round-trip means a second
        Observe for this alias cannot even start probing until the first
        one's entire cycle has committed and released it, so whichever
        cycle starts second is always the one reflecting the more current
        remote state.

        Callers that already run detect_capability() themselves (or have
        evidence from elsewhere) should still go through
        record_observation() directly — that one is unaffected by this
        method and remains correct for a single, non-overlapping call."""
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:observe:{alias}",),
            )
            observation, observed_at, physical_identity = detect_capability(
                connection_url,
                allowed_relations=allowed_relations,
                tls_policy=tls_policy,
                version_relation=version_relation,
            )
            self._persist_observation(
                cur, alias, observation, connection_url, physical_identity,
                observed_at,
            )
            return self._get_with_cursor(cur, alias)

    def _persist_observation(
        self,
        cur,
        alias: str,
        observation: dict[str, Any],
        connection_url: str,
        physical_identity: str | None,
        observed_at: datetime,
    ) -> None:
        """The write half of record_observation()/observe() — takes an
        already-open cursor so observe() can run it inside the same
        transaction as its advisory lock. `alias` must already be
        validated. Raises FileNotFoundError if the alias doesn't exist."""
        reachable = observation["connectivity"] == "reachable"
        connection_identity = self._connection_identity(connection_url)
        incoming_fingerprint = observation.get("schemaFingerprint")
        if incoming_fingerprint is not None:
            # FOR UPDATE — without it, this plain read can see a pre-
            # commit value while a concurrent provision() call is mid-
            # transaction (provision() takes its own FOR UPDATE lock on
            # this same row for its whole duration): this observation
            # would then compute acceptedSchemaCurrent against a
            # fingerprint provision() is about to replace, rather than
            # the one it actually commits. Only acceptedSchemaCurrent
            # depends on this read — never a security/correctness gate,
            # since provision()'s own comparison always re-reads live —
            # but there is no reason to leave even a reporting field
            # racy when closing it is a one-clause change: this lock
            # simply makes the write below wait for a concurrent
            # provision() to finish first, exactly as the UPDATE further
            # down already does.
            cur.execute(
                sql.SQL(
                    "SELECT accepted_schema_fingerprint FROM {}._aliases "
                    "WHERE alias = %s FOR UPDATE"
                ).format(sql.Identifier(SCHEMA)),
                (alias,),
            )
            existing = cur.fetchone()
            accepted_fingerprint = (
                existing["accepted_schema_fingerprint"] if existing else None
            )
            observation = {
                **observation,
                "acceptedSchemaCurrent": (
                    accepted_fingerprint is None
                    or accepted_fingerprint == incoming_fingerprint
                ),
            }
        # Appended unconditionally, regardless of whether this
        # observation goes on to win the latest-observation race below
        # — a lost race is still a real fact about what this probe saw
        # and when. The WHERE EXISTS guard makes this a silent no-op
        # for a nonexistent alias rather than a foreign-key violation;
        # the conditional UPDATE below still raises FileNotFoundError.
        cur.execute(
            sql.SQL("""
                INSERT INTO {}._observations
                  (alias, observation, connection_identity,
                   physical_identity, observed_at)
                SELECT %s, %s, %s, %s, %s
                WHERE EXISTS (
                  SELECT 1 FROM {}._aliases WHERE alias = %s
                )
            """).format(sql.Identifier(SCHEMA), sql.Identifier(SCHEMA)),
            (
                alias,
                Jsonb(observation),
                connection_identity,
                physical_identity,
                observed_at,
                alias,
            ),
        )
        cur.execute(
            sql.SQL("""
                UPDATE {}._aliases
                SET last_observation = %s,
                    last_observed_connection_identity = %s,
                    physical_identity = %s,
                    observed_at = %s,
                    status = CASE
                      WHEN provisioned_at IS NULL THEN status
                      WHEN %s THEN 'active'
                      ELSE 'unavailable'
                    END
                WHERE alias = %s
                  AND (
                    observed_at IS NULL
                    OR (last_observation ->> 'connectivity' = 'reachable')
                         <> %s
                    OR observed_at < %s
                  )
                RETURNING alias, provisioned_at
            """).format(sql.Identifier(SCHEMA)),
            (
                Jsonb(observation),
                connection_identity,
                physical_identity,
                observed_at,
                reachable,
                alias,
                reachable,
                observed_at,
            ),
        )
        row = cur.fetchone()
        if row is None:
            # Either this alias doesn't exist, or a newer Observe already
            # committed while this one's remote probe was still running
            # (observe() closes that specific race — see its docstring —
            # but record_observation() remains directly callable by a
            # caller running its own probe outside that serialization).
            # The latter isn't a failure — the row already holds the
            # fresher result — so only raise once non-existence is
            # confirmed.
            cur.execute(
                sql.SQL("SELECT 1 FROM {}._aliases WHERE alias = %s").format(
                    sql.Identifier(SCHEMA)
                ),
                (alias,),
            )
            if cur.fetchone() is None:
                raise FileNotFoundError(alias)
            return
        # A version drift discovered after provisioning must fail
        # pushdown closed immediately (docs/federation-architecture-
        # waypoint.md: "a version drift detected after provisioning
        # downgrades the alias to pushdown disabled") — re-enabling it
        # afterward is a deliberate federation:provision-scoped
        # reprovisioning action, never automatic, so this only ever
        # drops the option here, never adds it.
        if row["provisioned_at"] is not None:
            server_name = f"{alias}_srv"
            local_versions = extension_versions(cur)
            remote_versions = observation.get("extensionVersions") or {}
            shippable = self._shippable_extensions(
                local_versions, remote_versions
            )
            current = self._current_shippable_extensions(cur, server_name)
            if current and not shippable:
                self._apply_shippable_extensions(
                    cur, server_name, current, shippable
                )

    # PostGIS/PROJ/GEOS versions may differ between the federation database
    # and a source — execution happens in the federation database, so a
    # pushed-down expression (e.g. ST_Transform) can silently return a
    # different result from the same expression evaluated locally if the
    # versions disagree. postgisExtversion is a separate, additional
    # requirement, not a replacement for the library-version check above:
    # a same-library, different-extversion pair still evaluates shared
    # expressions identically, but the older SQL script on one side may be
    # missing a function or operator the newer one added — pushing down an
    # expression that uses it would fail at the SQL level rather than
    # merely evaluate differently (see extension_versions()'s docstring).
    # Only ever mark postgis shippable when every one of these exactly
    # matches the alias's last observation of the remote.
    _VERSION_MATCH_KEYS = ("postgis", "postgisExtversion", "proj", "geos")

    @staticmethod
    def _shippable_extensions(
        local_versions: dict[str, str], remote_versions: dict[str, str]
    ) -> list[str]:
        matches = all(
            local_versions.get(key) and local_versions.get(key) == remote_versions.get(key)
            for key in FederationAliasStore._VERSION_MATCH_KEYS
        )
        return ["postgis"] if matches else []

    @staticmethod
    def _current_shippable_extensions(cur, server_name: str) -> list[str]:
        cur.execute(
            "SELECT srvoptions FROM pg_catalog.pg_foreign_server "
            "WHERE srvname = %s",
            (server_name,),
        )
        row = cur.fetchone()
        for option in (row["srvoptions"] if row and row["srvoptions"] else []):
            key, _, value = option.partition("=")
            if key == "extensions":
                return [name for name in value.split(",") if name]
        return []

    @staticmethod
    def _apply_shippable_extensions(
        cur, server_name: str, current: list[str], desired: list[str]
    ) -> None:
        if current == desired:
            return
        if current:
            cur.execute(
                sql.SQL("ALTER SERVER {} OPTIONS (DROP extensions)").format(
                    sql.Identifier(server_name)
                )
            )
        if desired:
            cur.execute(
                sql.SQL("ALTER SERVER {} OPTIONS (ADD extensions {})").format(
                    sql.Identifier(server_name),
                    sql.Literal(",".join(desired)),
                )
            )

    # libpq settings from the connectionRef that must be reproduced on the
    # foreign server, because postgres_fdw opens its own connection and would
    # otherwise fall back to libpq defaults. hostaddr belongs here with the
    # TLS settings: when a connectionRef supplies it, Observe and
    # verify_remote_state() connect to THAT address, so a server built from
    # host alone can resolve somewhere else entirely — the identity, schema,
    # and privileges that were verified would then describe a different
    # database than the one runtime queries actually reach. (host is still
    # sent too, since with hostaddr present libpq uses host purely for TLS
    # name verification.) Not user/password: those live in the user mapping.
    _FORWARDED_CONNECTION_OPTIONS = (
        "hostaddr", "sslmode", "sslrootcert", "sslcert", "sslkey",
    )

    @staticmethod
    def _current_server_options(cur, server_name: str) -> dict[str, str]:
        cur.execute(
            "SELECT srvoptions FROM pg_catalog.pg_foreign_server "
            "WHERE srvname = %s",
            (server_name,),
        )
        row = cur.fetchone()
        options: dict[str, str] = {}
        for option in (row["srvoptions"] if row and row["srvoptions"] else []):
            key, _, value = option.partition("=")
            options[key] = value
        return options

    @classmethod
    def _reconcile_forwarded_options(
        cls, cur, server_name: str, current: dict[str, str], params
    ) -> None:
        """Reconcile _FORWARDED_CONNECTION_OPTIONS to match the
        connectionRef's current connection string — a rotated setting (e.g.
        sslmode require -> verify-full, a renewed CA/client certificate, a
        changed hostaddr, or any of them removed outright) must reach the
        live server the same way a rotated host/password does, or
        foreign-table queries keep using stale settings: possibly weaker
        transport, or an address that no longer hosts the verified
        database."""
        actions = []
        for option_name in cls._FORWARDED_CONNECTION_OPTIONS:
            value = params.get(option_name)
            if value:
                verb = "SET" if option_name in current else "ADD"
                actions.append(
                    sql.SQL("{} {} {}").format(
                        sql.SQL(verb),
                        sql.Identifier(option_name),
                        sql.Literal(str(value)),
                    )
                )
            elif option_name in current:
                actions.append(
                    sql.SQL("DROP {}").format(sql.Identifier(option_name))
                )
        if actions:
            cur.execute(
                sql.SQL("ALTER SERVER {} OPTIONS ({})").format(
                    sql.Identifier(server_name), sql.SQL(", ").join(actions)
                )
            )

    @staticmethod
    def _import_and_grant(
        cur,
        alias: str,
        server_name: str,
        schema_name: str,
        allowed_relations,
        reader_role: str,
    ) -> None:
        """IMPORT FOREIGN SCHEMA each allowed relation individually (not a
        blanket import), confirm every one actually landed, then grant the
        reader role access to the schema and everything just imported into
        it. Shared by first-time provisioning and by drift reconciliation
        during reprovisioning (see provision()) — both need the identical
        all-or-nothing behaviour: IMPORT FOREIGN SCHEMA ... LIMIT TO
        silently imports whatever of the named relations actually exists on
        the remote rather than erroring on one that's missing, so without
        the explicit check below, an alias could be left active with an
        incomplete set of foreign tables."""
        for relation in allowed_relations:
            remote_schema, remote_table = relation.split(".", 1)
            cur.execute(sql.SQL("""
                IMPORT FOREIGN SCHEMA {remote_schema}
                LIMIT TO ({table})
                FROM SERVER {server}
                INTO {local_schema}
            """).format(
                remote_schema=sql.Identifier(remote_schema),
                table=sql.Identifier(remote_table),
                server=sql.Identifier(server_name),
                local_schema=sql.Identifier(schema_name),
            ))
        cur.execute(
            sql.SQL("""
                SELECT c.relname
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind = 'f'
            """),
            (schema_name,),
        )
        imported = {row["relname"] for row in cur.fetchall()}
        expected = {relation.split(".", 1)[1] for relation in allowed_relations}
        missing = sorted(expected - imported)
        if missing:
            raise FederationSchemaError(
                f"Alias {alias!r} provisioning did not import: "
                f"{missing} — the source schema may have changed "
                "since it was last observed.",
                code="federation.import_incomplete",
            )
        cur.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema_name), sql.Identifier(reader_role)
            )
        )
        cur.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema_name), sql.Identifier(reader_role)
            )
        )

    @staticmethod
    def _connection_identity(connection_url: str) -> str:
        """The remote endpoint and login role a connectionRef currently
        resolves to (host/port/dbname/user — deliberately not password,
        a credential rather than an identity: re-authenticating the same
        role is already reconciled on every provision/reprovision call
        regardless of this check). Observe and Provision each resolve
        connectionRef independently, at their own call time (app.py's
        resolve_federation_connection_url); binding every observation to
        the endpoint and role it actually reached lets Provision detect a
        connectionRef rotated to a different endpoint OR a different
        remote user since the last Observe — has_table_privilege and any
        row-level security the source enforces are evaluated per-role, so
        a role change alone can invalidate a "current" schema flag just as
        much as a host change can."""
        params = psycopg.conninfo.conninfo_to_dict(connection_url)
        host = str(params.get("host", ""))
        port = str(params.get("port", "5432"))
        dbname = str(params.get("dbname", ""))
        user = str(params.get("user", ""))
        return f"{user}@{host}:{port}/{dbname}"

    def provision(
        self,
        alias: str,
        connection_url: str,
        actor: str,
        *,
        acknowledge_row_level_security: bool = False,
        acknowledge_schema_change: bool = False,
        acknowledge_physical_rebind: bool = False,
    ) -> dict[str, Any]:
        """Create the real FDW server, user mapping, schema, and foreign
        tables for this alias's allowedRelations the first time this is
        called. Callable again afterward as a reprovisioning action (e.g.
        an admin acting on a drift observation) — a repeat call normally
        only re-decides and reconciles the server's `extensions` option; it
        does not repeat CREATE SERVER/USER MAPPING, which would fail on
        objects that already exist. It DOES repeat IMPORT FOREIGN SCHEMA
        (via DROP FOREIGN TABLE + reimport) when acknowledging a schema
        change, since that is the only way the local foreign table's
        columns/types ever catch up with the remote's — see the
        acknowledge_schema_change paragraph below. `actor` is the
        approving principal — every call (initial or reprovisioning) is
        itself an act of Approve exposure, recorded atomically with
        activation as the docs/federation-architecture-waypoint.md source-
        alias contract's approvedBy/approvedAt consent record.

        `acknowledge_schema_change` mirrors `acknowledge_row_level_security`
        exactly, for the same reason: `accepted_schema_fingerprint` is a
        durable column this method alone ever writes (never Observe), so a
        live fingerprint that no longer matches it means the source's
        columns or view definition changed since the *last explicit
        acceptance* — not merely since the last Observe, which self-heals
        the comparison the moment anyone observes twice with no further
        changes (see record_observation()'s docstring). A caller must
        explicitly accept that change to proceed, exactly as with RLS.
        On an already-provisioned alias, acknowledging a genuine change
        also DROPs and reimports the foreign tables for every allowed
        relation — a foreign table's columns/types are fixed at IMPORT
        time and never auto-update, so persisting the new accepted
        fingerprint without this would silence the warning forever while
        every runtime query kept using the stale local definition. This
        reuses the exact IMPORT FOREIGN SCHEMA DDL first-time provisioning
        already uses (see _import_and_grant) rather than hand-diffing
        column lists; a relation a derived layer already depends on fails
        the DROP outright (pg_depend) and rolls back this whole call
        instead of silently reconciling around a dependent object — an
        informed stop, consistent with this module's own impact-visibility
        design, not a gap.

        `acknowledge_physical_rebind` is the third gate in the same family,
        guarding `accepted_physical_identity`: the source being a *different
        physical database* than the one previously accepted. The doc treats
        this as its own class of change — "The same table name at a new
        physical database is not the same source unless an operator performs
        an explicit, evidenced rebind" — and it is genuinely not covered by
        the other two, since a restored or recreated database can present a
        byte-identical schema fingerprint and satisfy every RLS check while
        serving entirely different rows.

        Every successful call, whether or not any acknowledgement was needed
        this time, re-persists accepted_schema_fingerprint and
        accepted_physical_identity as the new baselines."""
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            # Locks the row for the remainder of this call — a concurrent
            # Observe attempting to record a newer observation on this
            # same alias blocks until this transaction commits or rolls
            # back. Without this, a concurrent Observe could commit a
            # worse observation (schema "changed", or a version drift
            # that should disable pushdown) after this reads the old
            # evidence but before the FDW changes below commit, letting
            # Provision report success — or even re-enable pushdown —
            # against evidence Observe had already superseded.
            cur.execute(
                sql.SQL("""
                    SELECT {}, last_observed_connection_identity,
                           physical_identity, accepted_schema_fingerprint,
                           accepted_physical_identity
                    FROM {}._aliases
                    WHERE alias = %s
                    FOR UPDATE
                """).format(self._SELECT_COLUMNS, sql.Identifier(SCHEMA)),
                (alias,),
            )
            row = cur.fetchone()
            if row is None:
                raise FileNotFoundError(alias)
            record = dict(row)
            last_observed_connection_identity = record.pop(
                "last_observed_connection_identity"
            )
            last_observed_physical_identity = record.pop("physical_identity")
            accepted_schema_fingerprint = record.pop("accepted_schema_fingerprint")
            accepted_physical_identity = record.pop("accepted_physical_identity")
            already_provisioned = record["provisionedAt"] is not None
            last_observation = record.get("lastObservation") or {}
            # Every MAPP caller queries a federated source through the same
            # mapped remote user (there is only one connectionRef per alias),
            # so any row-level security the source enforces per connecting
            # user is bypassed entirely once federated — the mapped role's
            # full row set becomes visible to every runtime caller. The
            # generic dataHandlingAcknowledged at registration cannot cover
            # this: RLS is only discovered later, by Observe. This is a
            # fast-fail using the *stored* evidence only — it saves a live
            # remote round-trip in the common case where the last Observe
            # already showed RLS and nothing acknowledged it since; it is
            # NOT the authoritative gate (verify_remote_state's live re-check
            # below is — RLS could just as easily have been enabled *after*
            # this Observe, which this check alone could never catch).
            if last_observation.get("rowLevelSecurityDetected") and not (
                acknowledge_row_level_security
            ):
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source has row-level security that "
                    "would be bypassed once federated — acknowledge this "
                    "explicitly to provision.",
                    code="federation.row_level_security_not_acknowledged",
                )
            # Applies to reprovisioning too, not just the first call: the
            # reprovision branch below never re-verifies or re-imports the
            # foreign tables, so without this, /provision on an alias whose
            # latest observation reports "changed" would still reconcile
            # connection settings and report success — silently implying
            # the schema drift it never actually addressed was fine.
            if last_observation.get("schema") != "current":
                raise FederationSchemaError(
                    f"Alias {alias!r} has not been observed as current — "
                    "observe it again immediately before provisioning.",
                    code="federation.observation_not_current",
                )
            # A "current" schema above only proves *some* past Observe call
            # was current — not that it was taken against the endpoint and
            # remote role this call is about to provision. If connectionRef's
            # DBS_<NAME> was rotated to a different host/database, or to a
            # different remote login role on the same host/database, since
            # that Observe (e.g. the service restarted with a new value), this
            # call would otherwise activate identically-named relations, or a
            # different row set, that were never observed or reviewed under
            # the role about to be wired up.
            connection_identity = self._connection_identity(connection_url)
            if last_observed_connection_identity != connection_identity:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s connectionRef now resolves to a "
                    "different endpoint or remote role than its last "
                    "observation was taken against — observe it again before "
                    "provisioning.",
                    code="federation.observation_not_current",
                )
            # The registered tlsPolicy is an attestation the operator made at
            # registration time — without this, nothing ever checked that the
            # connectionRef this alias actually resolves to delivers it, so a
            # registered "verify-full" alias could be provisioned over
            # plaintext without ever being flagged.
            enforce_tls_policy(record["tlsPolicy"], connection_url)
            # connection_identity alone can't catch a database dropped,
            # restored, or replaced in place — that keeps the exact same
            # host/port/dbname/user (docs/federation-architecture-waypoint.md,
            # "Drift and retirement": "Different physical database | Raise
            # identity conflict; require explicit rebind"). Re-fetch the
            # remote's live physical identity, extension versions, relation
            # existence/selectability, RLS/security-barrier exposure, and
            # column/view-definition fingerprint now, all from this one
            # connection — see federation_capability.verify_remote_state's
            # docstring for why every decision below must use evidence from
            # this single live snapshot, never a stored value or a second,
            # separate connection.
            try:
                (
                    current_physical_identity,
                    live_remote_versions,
                    relations_verified,
                    rls_detected,
                    current_schema_fingerprint,
                ) = verify_remote_state(
                    connection_url, tuple(record["allowedRelations"])
                )
            except psycopg.Error as exc:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source could not be reached to verify "
                    "its physical identity before provisioning — observe it "
                    "again.",
                    code="federation.observation_not_current",
                ) from exc
            # The stored last_observation.schema == "current" check above
            # only proves this was true as of the last Observe — none of
            # Observe's own evidence-collecting checks (existence,
            # selectability, RLS/security-barrier, or column/view
            # definition) are re-verified anywhere else, so without this,
            # a relation dropped, a SELECT revoked, RLS newly enabled, or a
            # column added / view redefined since that Observe would all be
            # silently invisible to Provision (physical_identity alone
            # covers none of them — none change a relation's own oid).
            if not relations_verified:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source's allowed relations are no "
                    "longer all present and selectable by the connecting "
                    "role — observe it again and review before "
                    "provisioning.",
                    code="federation.observation_not_current",
                )
            # The authoritative RLS gate — unlike the stored-evidence fast-
            # fail above, this catches RLS/security-barrier exposure
            # enabled on the remote at any point up to this live snapshot,
            # not just what the last Observe happened to see.
            if rls_detected and not acknowledge_row_level_security:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source has row-level security that "
                    "would be bypassed once federated — acknowledge this "
                    "explicitly to provision.",
                    code="federation.row_level_security_not_acknowledged",
                )
            # Persisted atomically with approval below — a durable record
            # that the approving principal explicitly accepted the
            # per-user-RLS bypass this source has *as of this live check*,
            # not just a transient gate that leaves no trace once a later
            # Observe replaces last_observation (the append-only
            # _observations table records what was detected, but not that
            # it was accepted).
            rls_bypass_acknowledged = rls_detected and acknowledge_row_level_security
            # Staleness: the evidence being acted on must have been collected
            # against the same physical database this call is about to wire
            # up. Escape hatch is a fresh Observe.
            if last_observed_physical_identity != current_physical_identity:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source's physical database identity "
                    "no longer matches its last observation — it may have "
                    "been dropped, restored, or replaced. Observe it again "
                    "and review before provisioning.",
                    code="federation.observation_not_current",
                )
            # Acceptance: the live database must still be the one a previous
            # successful provisioning actually accepted. The staleness check
            # above cannot cover this — it compares against physical_identity,
            # which every Observe overwrites, so a source that was replaced
            # and *then* observed matches itself and sails through (verified
            # live: a dropped-and-recreated relation kept an identical schema
            # fingerprint, passed every other gate, and served replacement
            # rows through the existing foreign table). The doc is explicit
            # that this is not the same source: "The same table name at a new
            # physical database is not the same source unless an operator
            # performs an explicit, evidenced rebind"
            # (docs/federation-architecture-waypoint.md). None means nothing
            # has been accepted yet, i.e. this alias's first provisioning.
            if (
                accepted_physical_identity is not None
                and accepted_physical_identity != current_physical_identity
                and not acknowledge_physical_rebind
            ):
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source is a different physical "
                    "database than the one previously accepted — it was "
                    "dropped, restored, or replaced. Acknowledge this "
                    "rebind explicitly to provision.",
                    code="federation.physical_rebind_not_acknowledged",
                )
            # physical_identity only proves the relation itself wasn't
            # dropped/recreated — an ADD COLUMN or a CREATE OR REPLACE VIEW
            # that narrows or drops a row-filtering predicate changes
            # neither a relation's oid nor its RLS flags, so without this,
            # either could silently reach every runtime caller through the
            # blanket GRANT SELECT below, never having been reviewed.
            # Compared against accepted_schema_fingerprint (durable, set
            # only by a prior successful provision() below), not against
            # last_observation — the latter is exactly what every Observe
            # overwrites, including one that itself reports the drift, so
            # comparing against it would self-heal the moment anyone
            # observes twice with nothing further changed. None means
            # nothing has been accepted yet (this alias's first
            # provisioning) — handled separately just below, since there
            # is nothing yet to acknowledge past.
            schema_fingerprint_changed = (
                accepted_schema_fingerprint is not None
                and accepted_schema_fingerprint != current_schema_fingerprint
            )
            # On a first-ever provisioning, accepted_schema_fingerprint is
            # always None, so the check above is vacuously false — nothing
            # yet compares the live fingerprint against what the last
            # Observe actually saw. Without this, a column added or a view
            # redefined between Observe and this call would import
            # unreviewed: relations_verified and physical_identity both
            # stay true (neither changes on an ADD COLUMN or a same-object
            # CREATE OR REPLACE VIEW), so nothing else here would catch it.
            # last_observation.get("schema") == "current" already passed
            # above, and detect_capability() always sets schemaFingerprint
            # alongside schema in its reachable branch, so it's guaranteed
            # present here. This is deliberately observation_not_current,
            # not schema_change_not_acknowledged: the human's last Observe
            # is the only thing ever reviewed for a first provisioning, so
            # a live mismatch means that review is stale — there is
            # nothing yet accepted to knowingly acknowledge past, only
            # fresher evidence to go collect.
            if (
                accepted_schema_fingerprint is None
                and last_observation.get("schemaFingerprint")
                != current_schema_fingerprint
            ):
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source's relation definitions "
                    "(columns or view query) no longer match its last "
                    "observation — observe it again immediately before "
                    "provisioning.",
                    code="federation.observation_not_current",
                )
            if schema_fingerprint_changed and not acknowledge_schema_change:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source's relation definitions "
                    "(columns or view query) no longer match what was "
                    "last explicitly accepted — acknowledge this "
                    "explicitly to provision.",
                    code="federation.schema_change_not_acknowledged",
                )
            params = psycopg.conninfo.conninfo_to_dict(connection_url)
            host = str(params.get("host", ""))
            port = str(params.get("port", "5432"))
            dbname = str(params.get("dbname", ""))
            user = str(params.get("user", ""))
            password = str(params.get("password", ""))
            server_name = f"{alias}_srv"
            schema_name = f"source_{alias}"

            cur.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")

            # postgres_fdw only ships operators/functions to the remote
            # side for extensions explicitly marked "shippable" — PostGIS
            # isn't in its built-in list, so spatial predicates would
            # otherwise pull the whole remote relation and filter locally.
            # Only mark it shippable when the federation database's own
            # PostGIS/PROJ/GEOS versions exactly match the remote's *live*
            # versions, just re-verified above — an in-place extension
            # upgrade on the remote changes no OID at all, so the physical-
            # identity check above can't catch a version drift since
            # Observe, and comparing against Observe's stored
            # lastObservation.extensionVersions instead of this live value
            # would risk enabling pushdown against a version the remote no
            # longer actually has. An unconditional claim would risk a
            # silently wrong pushed-down result, not just a remote
            # execution error. This is a same-owner, non-superuser server
            # option; it does not duplicate any data, it only widens what
            # may cross the wire as a pushed-down predicate instead of a
            # full relation.
            local_versions = extension_versions(cur)
            shippable = self._shippable_extensions(
                local_versions, live_remote_versions
            )

            if already_provisioned:
                # A reprovisioning call is also the only place a rotated
                # connectionRef (new password, host, or database behind the
                # same alias) ever reaches the live foreign server —
                # reconcile it every time, not just the extensions option.
                cur.execute(sql.SQL("""
                    ALTER SERVER {server} OPTIONS (
                        SET host {host}, SET port {port}, SET dbname {dbname}
                    )
                """).format(
                    server=sql.Identifier(server_name),
                    host=sql.Literal(host),
                    port=sql.Literal(port),
                    dbname=sql.Literal(dbname),
                ))
                current_server_options = self._current_server_options(
                    cur, server_name
                )
                self._reconcile_forwarded_options(
                    cur, server_name, current_server_options, params
                )
                cur.execute(sql.SQL("""
                    ALTER USER MAPPING FOR CURRENT_USER SERVER {server}
                    OPTIONS (SET user {user}, SET password {password})
                """).format(
                    server=sql.Identifier(server_name),
                    user=sql.Literal(user),
                    password=sql.Literal(password),
                ))
                # DROP + CREATE, not ALTER: an alias provisioned before the
                # reader mapping existed has no mapping for self.reader_role
                # yet to ALTER — this converges it the next time it's
                # reprovisioned, and is a no-op-then-recreate for one that
                # already has a mapping.
                cur.execute(sql.SQL("""
                    DROP USER MAPPING IF EXISTS FOR {reader} SERVER {server}
                """).format(
                    reader=sql.Identifier(self.reader_role),
                    server=sql.Identifier(server_name),
                ))
                cur.execute(sql.SQL("""
                    CREATE USER MAPPING FOR {reader} SERVER {server}
                    OPTIONS (user {user}, password {password})
                """).format(
                    reader=sql.Identifier(self.reader_role),
                    server=sql.Identifier(server_name),
                    user=sql.Literal(user),
                    password=sql.Literal(password),
                ))
                current = self._current_shippable_extensions(cur, server_name)
                self._apply_shippable_extensions(cur, server_name, current, shippable)
                if schema_fingerprint_changed:
                    # Reaching here means acknowledge_schema_change was true
                    # (the gate above already raised otherwise) — the
                    # foreign tables' columns/types are fixed at IMPORT
                    # time and never auto-update, so persisting the new
                    # accepted fingerprint below without reconciling them
                    # would silence this warning forever while every
                    # runtime query kept using the stale local definition.
                    # See provision()'s docstring for why DROP + reimport,
                    # not ALTER FOREIGN TABLE.
                    for relation in record["allowedRelations"]:
                        _, remote_table = relation.split(".", 1)
                        cur.execute(
                            sql.SQL("DROP FOREIGN TABLE {}.{}").format(
                                sql.Identifier(schema_name),
                                sql.Identifier(remote_table),
                            )
                        )
                    self._import_and_grant(
                        cur,
                        alias,
                        server_name,
                        schema_name,
                        record["allowedRelations"],
                        self.reader_role,
                    )
                cur.execute(
                    sql.SQL("""
                        UPDATE {}._aliases
                        SET approved_by = %s, approved_at = clock_timestamp(),
                            row_level_security_acknowledged = %s,
                            accepted_schema_fingerprint = %s,
                            accepted_physical_identity = %s
                        WHERE alias = %s
                    """).format(sql.Identifier(SCHEMA)),
                    (
                        actor,
                        rls_bypass_acknowledged,
                        current_schema_fingerprint,
                        current_physical_identity,
                        alias,
                    ),
                )
            else:
                server_options = [
                    sql.SQL("host {}").format(sql.Literal(host)),
                    sql.SQL("port {}").format(sql.Literal(port)),
                    sql.SQL("dbname {}").format(sql.Literal(dbname)),
                    sql.SQL("use_remote_estimate 'true'"),
                ]
                # Forward whatever the operator already put in the
                # connectionRef's connection string (the repo's existing
                # sslmode/sslrootcert convention — see .env.example) instead of
                # letting libpq silently fall back to its own permissive
                # default. Same tuple the reprovision path reconciles against,
                # so create and reconcile cannot drift apart.
                for option_name in self._FORWARDED_CONNECTION_OPTIONS:
                    value = params.get(option_name)
                    if value:
                        server_options.append(
                            sql.SQL("{} {}").format(
                                sql.Identifier(option_name), sql.Literal(str(value))
                            )
                        )
                if shippable:
                    server_options.append(
                        sql.SQL("extensions {}").format(
                            sql.Literal(",".join(shippable))
                        )
                    )
                # Not IF NOT EXISTS: same reasoning as the schema below — a
                # not-yet-provisioned alias should never legitimately reach a
                # pre-existing <alias>_srv. Silently reusing one from an
                # unrelated origin (and possibly a different actual remote
                # endpoint) would import and grant access to the wrong data
                # under this alias's name. Fail closed.
                cur.execute(sql.SQL("""
                    CREATE SERVER {server}
                    FOREIGN DATA WRAPPER postgres_fdw
                    OPTIONS ({options})
                """).format(
                    server=sql.Identifier(server_name),
                    options=sql.SQL(", ").join(server_options),
                ))
                cur.execute(sql.SQL("""
                    CREATE USER MAPPING IF NOT EXISTS FOR CURRENT_USER
                    SERVER {server}
                    OPTIONS (user {user}, password {password})
                """).format(
                    server=sql.Identifier(server_name),
                    user=sql.Literal(user),
                    password=sql.Literal(password),
                ))
                # A security_invoker=true derived VIEW (derived_layers.py) runs
                # its underlying query as whichever role queries the view — the
                # runtime reader, not the derived owner — so it needs its own
                # mapping too, not just CURRENT_USER's. Both mappings use the
                # same remote credential; there is only one connectionRef per
                # alias. (A materialized view's REFRESH always runs as the
                # derived owner, so this isn't needed for that path, but views
                # are a supported derived-layer kind and must work too.)
                cur.execute(sql.SQL("""
                    CREATE USER MAPPING IF NOT EXISTS FOR {reader}
                    SERVER {server}
                    OPTIONS (user {user}, password {password})
                """).format(
                    reader=sql.Identifier(self.reader_role),
                    server=sql.Identifier(server_name),
                    user=sql.Literal(user),
                    password=sql.Literal(password),
                ))
                # Not IF NOT EXISTS: an alias's own reprovisioning never reaches
                # this branch (see already_provisioned above), so the only way
                # this schema could already exist here is an unrelated object —
                # e.g. a stray schema an operator created by hand that happens
                # to collide with source_<alias>. Silently reusing it would
                # import into and then GRANT SELECT ON ALL TABLES IN that
                # schema, exposing whatever was already there. Fail closed.
                cur.execute(
                    sql.SQL("CREATE SCHEMA {}").format(
                        sql.Identifier(schema_name)
                    )
                )
                # KNOWN GAP, deliberately not closed here (PR #25 review,
                # round 20): verify_remote_state() above closes its own
                # connection before this IMPORT FOREIGN SCHEMA runs, which
                # opens a separate FDW-managed connection to the remote —
                # DDL between the two could let this bind a replacement or
                # altered relation that the live verification above never
                # actually saw. Closing it would mean re-verifying physical
                # identity and schema fingerprint again after import,
                # before this transaction commits. Judged not worth the
                # added complexity for this test slice; revisit if this
                # module moves beyond a test slice.
                self._import_and_grant(
                    cur,
                    alias,
                    server_name,
                    schema_name,
                    record["allowedRelations"],
                    self.reader_role,
                )
                cur.execute(
                    sql.SQL("""
                        UPDATE {}._aliases
                        SET provisioned_at = clock_timestamp(), status = 'active',
                            approved_by = %s, approved_at = clock_timestamp(),
                            row_level_security_acknowledged = %s,
                            accepted_schema_fingerprint = %s,
                            accepted_physical_identity = %s
                        WHERE alias = %s
                    """).format(sql.Identifier(SCHEMA)),
                    (
                        actor,
                        rls_bypass_acknowledged,
                        current_schema_fingerprint,
                        current_physical_identity,
                        alias,
                    ),
                )
            return self._get_with_cursor(cur, alias)

    def affected_derived_layer_names(self, alias: str) -> list[str]:
        """Derived layers whose declared sources read from this alias's
        foreign schema (`source_<alias>`) — the impact-visibility query."""
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            # derived_layers._definitions is created lazily by
            # DerivedLayerStore._initialize() on first use, not by
            # database init — a fresh deployment that registered a
            # federation alias but never called a derived-layer endpoint
            # has the `derived_layers` schema (init script) but not yet
            # this table. An absent table has no rows to depend on any
            # alias, so it's an empty dependency set, not an error.
            cur.execute(
                "SELECT to_regclass('derived_layers._definitions') AS oid"
            )
            if cur.fetchone()["oid"] is None:
                return []
            cur.execute(
                sql.SQL("""
                    SELECT name FROM {}._definitions
                    WHERE EXISTS (
                      SELECT 1 FROM unnest(sources) AS source
                      WHERE split_part(source, '.', 1) = %s
                    )
                    ORDER BY name
                """).format(sql.Identifier(DERIVED_LAYERS_SCHEMA)),
                (f"source_{alias}",),
            )
            return [row["name"] for row in cur.fetchall()]
