"""Transactional semantic metadata storage.

The store deliberately knows nothing about HTTP or source databases.  It owns
the small, durable contract needed by the private semantic service:

* generated metadata can only be changed by idempotent source events;
* curated metadata can only be changed by revision-bound proposals; and
* catalog readers see immutable snapshots identified by a global revision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


SCHEMA_VERSION = 4
MAX_ID_LENGTH = 200
MAX_OPERATIONS = 100
MAX_CURATED_FIELDS_BYTES = 1024 * 1024
MAX_FIELD_ANNOTATION_BYTES = 16 * 1024
MAX_FIELD_ANNOTATION_PROPERTIES = 64
MAX_COLLECTION_FETCH = 101
_MISSING = object()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SCHEMA = "semantic"

# Both semantic roles are created with CONNECTION LIMIT 4 in
# docker/postgis/init/10-roles.sh, and the server is a ThreadingHTTPServer that
# opens one connection per request with no pool. Without this the fifth
# concurrent request -- reachable from a single supported dashboard action, and
# from eight parallel healthchecks -- is refused by PostgreSQL with "too many
# connections for role" and surfaces as an HTTP 500. Requests queue here
# instead. One slot below the role limit is left free deliberately, so an
# operator can still psql in as the role while the service is saturated.
MAX_CONCURRENT_CONNECTIONS = 3
# One name for the store-wide advisory lock that stands in for SQLite's
# BEGIN IMMEDIATE.  Every writer that publishes a new catalog revision takes
# it first, so those writers never interleave and can never acquire the
# metadata and asset row locks in opposing orders.
STORE_LOCK = "semantic"

# Rows arrive as dictionaries, so `row["asset_id"]` reads exactly as it did
# under sqlite3.Row.
StoreConnection = psycopg.Connection[dict[str, Any]]


class SemanticError(Exception):
    """Expected client-visible failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticError("invalid_request", f"{name} must be a JSON object.")
    return value


def _require_string(value: Any, name: str, *, maximum: int = MAX_ID_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SemanticError(
            "invalid_request",
            f"{name} must be a non-blank string of at most {maximum} characters.",
        )
    return value


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SemanticError("invalid_request", f"{name} must be a positive integer.")
    return int(value)


def _closed_object(
    value: dict[str, Any], allowed: set[str], name: str = "request"
) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise SemanticError(
            "invalid_request",
            f"{name} contains unsupported properties.",
            details={"properties": unexpected},
        )


class SemanticStore:
    def __init__(
        self,
        database_url: str,
        reader_database_url: str,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.database_url = database_url
        self.reader_database_url = reader_database_url
        self.clock = clock
        # One gate per role rather than one shared gate: a request holds a
        # single connection at a time, so separate gates cannot deadlock
        # against each other, and a saturated reader never blocks a write.
        self._writer_slots = threading.BoundedSemaphore(
            MAX_CONCURRENT_CONNECTIONS
        )
        self._reader_slots = threading.BoundedSemaphore(
            MAX_CONCURRENT_CONNECTIONS
        )
        self._migrate()

    @staticmethod
    def _open(database_url: str) -> StoreConnection:
        connection = psycopg.connect(
            database_url,
            autocommit=True,
            connect_timeout=5,
            row_factory=dict_row,
        )
        try:
            # pg_catalog first, so nothing in the semantic schema can shadow a
            # built-in, and the semantic schema after it, so every unqualified
            # table name below resolves there.  Both login roles carry the same
            # setting; this repeats it because the store's SQL is only correct
            # under it.  Autocommit is on and transactions are opened by hand,
            # which is what sqlite3's isolation_level=None gave and what lets
            # read_snapshot state its own isolation level.
            connection.execute(
                sql.SQL("SET SESSION search_path = pg_catalog, {schema}").format(
                    schema=sql.Identifier(SCHEMA)
                )
            )
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self) -> Iterator[StoreConnection]:
        """Connect as the role allowed to write the semantic schema."""
        with self._slot(self._writer_slots, self.database_url) as connection:
            yield connection

    @contextmanager
    def _reader_connection(self) -> Iterator[StoreConnection]:
        """Connect as the role allowed only to read the semantic schema.

        Reads use their own login role so that a mistake on a read path is
        refused by PostgreSQL rather than only by this module.
        """
        with self._slot(self._reader_slots, self.reader_database_url) as connection:
            yield connection

    @contextmanager
    def _slot(
        self,
        slots: threading.BoundedSemaphore,
        database_url: str,
    ) -> Iterator[StoreConnection]:
        """Hold one of the role's connection slots for the whole session."""
        slots.acquire()
        try:
            connection = self._open(database_url)
        except BaseException:
            slots.release()
            raise
        try:
            yield connection
        finally:
            try:
                connection.close()
            finally:
                slots.release()

    @staticmethod
    def _lock_store(connection: StoreConnection) -> None:
        """Serialise catalogue-wide writers, as BEGIN IMMEDIATE used to.

        Held until the transaction ends.  Only the writers that publish a new
        catalog revision take it, so they run one at a time and the row locks
        they take afterwards are always acquired in the same order.
        """
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (STORE_LOCK,)
        )

    @contextmanager
    def read_snapshot(
        self,
    ) -> Iterator[tuple[StoreConnection, int]]:
        """Yield one connection pinned to the returned catalog revision."""
        with self._reader_connection() as connection:
            # REPEATABLE READ deliberately, and not PostgreSQL's default.  A
            # paginated response is assembled from several statements on this
            # connection and every one of them has to see the revision
            # reported beside it; under READ COMMITTED each statement would
            # take its own snapshot and one response could be built out of two
            # catalogue revisions.  READ ONLY states the intent and lets the
            # server reject a stray write here.
            connection.execute(
                "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            try:
                revision = self.catalog_revision(connection)
                yield connection, revision
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                # One lock and one transaction for the whole ladder.
                # PostgreSQL DDL is transactional, so a second service
                # starting at the same moment waits here and then finds every
                # migration already recorded instead of racing CREATE TABLE.
                self._lock_store(connection)
                connection.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (
                            version bigint PRIMARY KEY,
                            applied_at text NOT NULL
                        )
                        """
                    ).format(schema=sql.Identifier(SCHEMA))
                )
                applied = {
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    )
                }
                if 1 not in applied:
                    self._migration_1(connection)
                if 2 not in applied:
                    self._migration_2(connection)
                if 3 not in applied:
                    self._migration_3(connection)
                if 4 not in applied:
                    self._migration_4(connection)
                if 5 not in applied:
                    self._migration_5(connection)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _record_migration(connection: StoreConnection, version: int) -> None:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(%s, %s)",
            (version, utc_now()),
        )

    @staticmethod
    def _migration_1(connection: StoreConnection) -> None:
        # The *_json columns are text rather than jsonb on purpose.  The store
        # fingerprints canonical JSON, and jsonb reorders object keys and
        # rewrites whitespace, which would change every stored fingerprint and
        # break the proposal evidence check in apply_proposal.
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE {schema}.metadata (
                    key text PRIMARY KEY,
                    value text NOT NULL
                );
                INSERT INTO {schema}.metadata(key, value)
                    VALUES('catalog_revision', '0');

                CREATE TABLE {schema}.assets (
                    asset_id text PRIMARY KEY,
                    version bigint NOT NULL CHECK(version >= 1),
                    generation bigint NOT NULL CHECK(generation >= 1),
                    status text NOT NULL CHECK(status IN ('ready', 'archived')),
                    visibility text NOT NULL
                        CHECK(visibility IN ('inspect', 'admin')),
                    generated_json text NOT NULL,
                    curated_json text NOT NULL,
                    orphans_json text NOT NULL,
                    catalog_revision bigint NOT NULL,
                    created_at text NOT NULL,
                    updated_at text NOT NULL,
                    archived_at text
                );

                CREATE TABLE {schema}.asset_history (
                    history_id bigint GENERATED BY DEFAULT AS IDENTITY
                        PRIMARY KEY,
                    asset_id text NOT NULL,
                    version bigint NOT NULL,
                    generation bigint NOT NULL,
                    catalog_revision bigint NOT NULL,
                    change_type text NOT NULL,
                    event_id text,
                    proposal_id text,
                    actor text NOT NULL,
                    snapshot_json text NOT NULL,
                    changed_at text NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES {schema}.assets(asset_id)
                );
                CREATE INDEX asset_history_asset_idx
                    ON {schema}.asset_history(asset_id, history_id);

                CREATE TABLE {schema}.processed_events (
                    event_id text PRIMARY KEY,
                    payload_hash text NOT NULL,
                    generation bigint NOT NULL,
                    response_json text NOT NULL,
                    processed_at text NOT NULL
                );

                CREATE TABLE {schema}.proposals (
                    proposal_id text PRIMARY KEY,
                    state text NOT NULL CHECK(
                        state IN ('pending', 'applied', 'declined')
                    ),
                    asset_id text NOT NULL,
                    base_version bigint NOT NULL,
                    operations_json text NOT NULL,
                    fingerprint text NOT NULL,
                    diff_json text NOT NULL,
                    explanation text,
                    actor text NOT NULL,
                    reason text,
                    created_at text NOT NULL,
                    updated_at text NOT NULL,
                    applied_version bigint,
                    FOREIGN KEY(asset_id) REFERENCES {schema}.assets(asset_id)
                );
                CREATE INDEX proposals_asset_idx
                    ON {schema}.proposals(asset_id, created_at);
                CREATE INDEX proposals_state_idx
                    ON {schema}.proposals(state, created_at);
                """
            ).format(schema=sql.Identifier(SCHEMA))
        )
        SemanticStore._record_migration(connection, 1)

    @staticmethod
    def _migration_2(connection: StoreConnection) -> None:
        connection.execute(
            sql.SQL(
                """
                ALTER TABLE {schema}.proposals ADD COLUMN decided_by text;
                ALTER TABLE {schema}.proposals ADD COLUMN decided_at text;
                """
            ).format(schema=sql.Identifier(SCHEMA))
        )
        SemanticStore._record_migration(connection, 2)

    @staticmethod
    def _migration_3(connection: StoreConnection) -> None:
        connection.execute(
            sql.SQL(
                """
                ALTER TABLE {schema}.assets
                    ADD COLUMN predecessor_asset_id text
                    REFERENCES {schema}.assets(asset_id);
                """
            ).format(schema=sql.Identifier(SCHEMA))
        )
        SemanticStore._record_migration(connection, 3)

    @staticmethod
    def _migration_4(connection: StoreConnection) -> None:
        connection.execute(
            sql.SQL(
                """
                CREATE INDEX IF NOT EXISTS proposals_created_page_idx
                    ON {schema}.proposals(created_at, proposal_id);
                CREATE INDEX IF NOT EXISTS proposals_asset_page_idx
                    ON {schema}.proposals(asset_id, created_at, proposal_id);
                CREATE INDEX IF NOT EXISTS proposals_state_page_idx
                    ON {schema}.proposals(state, created_at, proposal_id);
                """
            ).format(schema=sql.Identifier(SCHEMA))
        )
        SemanticStore._record_migration(connection, 4)

    @staticmethod
    def _validated_fetch_limit(fetch_limit: int | None) -> int | None:
        if fetch_limit is None:
            return None
        if (
            isinstance(fetch_limit, bool)
            or not isinstance(fetch_limit, int)
            or not 1 <= fetch_limit <= MAX_COLLECTION_FETCH
        ):
            raise SemanticError(
                "invalid_request",
                f"fetch limit must be an integer from 1 to {MAX_COLLECTION_FETCH}.",
            )
        return fetch_limit

    def database_settings(self) -> dict[str, Any]:
        """Report the applied schema version.

        The journal mode, foreign-key, synchronous and busy-timeout keys this
        returned under SQLite described PRAGMAs that no longer exist; they had
        no PostgreSQL counterpart worth reporting and nothing read them.  The
        one key `status()` publishes is unchanged.
        """
        with self._reader_connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS schema_version"
                " FROM schema_migrations"
            ).fetchone()
            return {"schemaVersion": row["schema_version"]}

    def catalog_revision(self, connection: StoreConnection | None = None) -> int:
        if connection is not None:
            return int(
                connection.execute(
                    "SELECT value FROM metadata WHERE key = 'catalog_revision'"
                ).fetchone()["value"]
            )
        with self._reader_connection() as own_connection:
            return self.catalog_revision(own_connection)

    def _next_catalog_revision(self, connection: StoreConnection) -> int:
        # Reads the row FOR UPDATE rather than through catalog_revision, which
        # must stay usable inside a read-only transaction.  SQLite's
        # BEGIN IMMEDIATE serialised every writer and PostgreSQL does not:
        # without the row lock two transactions could read the same revision
        # and both write its successor, publishing two different catalogue
        # states under one number.
        current = connection.execute(
            "SELECT value FROM metadata WHERE key = 'catalog_revision'"
            " FOR UPDATE"
        ).fetchone()
        revision = int(current["value"]) + 1
        connection.execute(
            "UPDATE metadata SET value = %s WHERE key = 'catalog_revision'",
            (str(revision),),
        )
        return revision

    @staticmethod
    def _migration_5(connection: StoreConnection) -> None:
        """Record that an asset's underlying source is not currently usable.

        A separate column rather than a new `status` value, because the two
        facts are independent: `archived` is an operator's deliberate,
        confirmed decision, while this is an observation about the world that
        reverses itself when the source comes back. An archived asset whose
        source also vanished needs to carry both.

        NULL means the source is fine, which is what every existing row means.
        """
        connection.execute(
            sql.SQL(
                """
                ALTER TABLE {schema}.assets ADD COLUMN source_state text
                    CHECK(source_state IS NULL OR source_state = 'unavailable');
                """
            ).format(schema=sql.Identifier(SCHEMA))
        )
        SemanticStore._record_migration(connection, 5)

    @staticmethod
    def _asset_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["asset_id"],
            "version": row["version"],
            "generation": row["generation"],
            "status": row["status"],
            "visibility": row["visibility"],
            "generated": json.loads(row["generated_json"]),
            "curated": json.loads(row["curated_json"]),
            "orphans": json.loads(row["orphans_json"]),
            "catalogRevision": row["catalog_revision"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "archivedAt": row["archived_at"],
            "predecessorAssetId": row["predecessor_asset_id"],
            # Absent unless something is wrong, so a healthy catalogue reads
            # exactly as it did before.
            "sourceState": row["source_state"],
        }

    def mark_source_state(
        self, schema: str, *, available: bool
    ) -> list[str]:
        """Flag or clear every asset bound to one PostgreSQL schema.

        Keyed on the binding rather than on any federation identifier, so this
        stays a fact about where an asset reads from and needs no knowledge of
        aliases. Returns the assets it changed, so a caller can log once
        instead of on every pass.

        Deliberately not restricted to ready assets. An archived asset whose
        source disappears is still an archived asset whose source disappeared,
        and clearing the flag later must find it again -- filtering here would
        strand it flagged forever.
        """
        state = None if available else "unavailable"
        with self._connection() as connection:
            # One explicit write transaction over the select and the update.
            # The connection is opened with autocommit on, so without this each
            # statement would commit on its own: a reader could catch a schema
            # half marked, and an error midway would leave it that way
            # permanently. The observation is about the schema, so it has to
            # land for the whole schema or not at all.
            connection.execute("BEGIN")
            try:
                self._lock_store(connection)
                rows = connection.execute(
                    """
                    SELECT asset_id FROM assets
                    WHERE generated_json::jsonb -> 'binding' ->> 'schema' = %s
                      AND generated_json::jsonb -> 'binding' ->> 'adapter'
                          = 'postgresql'
                      AND source_state IS DISTINCT FROM %s
                    """,
                    (schema, state),
                ).fetchall()
                if not rows:
                    connection.execute("COMMIT")
                    return []
                changed = [row["asset_id"] for row in rows]
                # sourceState and updatedAt are API-visible, so this is a new
                # catalog snapshot and has to say so. Without a revision the
                # top-level number stays put while the payload underneath it
                # changes, and a pagination cursor minted before the change
                # stays valid across it -- letting a client assemble one
                # response out of two different snapshots. One revision for
                # the batch, stamped on every row it touched.
                revision = self._next_catalog_revision(connection)
                changed_at = utc_now()
                connection.execute(
                    "UPDATE assets SET source_state = %s, updated_at = %s, "
                    "catalog_revision = %s WHERE asset_id = ANY(%s)",
                    (state, changed_at, revision, changed),
                )
                connection.execute("COMMIT")
                return changed
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def assets_for_source_schema(self, schema: str) -> list[dict[str, Any]]:
        """Assets bound to one schema, whatever their status or source state."""
        with self._reader_connection() as connection:
            return [
                self._asset_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM assets
                    WHERE generated_json::jsonb -> 'binding' ->> 'schema' = %s
                      AND generated_json::jsonb -> 'binding' ->> 'adapter'
                          = 'postgresql'
                    ORDER BY asset_id
                    """,
                    (schema,),
                )
            ]

    @staticmethod
    def _visible(row: dict[str, Any], is_admin: bool) -> bool:
        return row["visibility"] == "inspect" or is_admin

    @classmethod
    def _asset_visible(cls, row: dict[str, Any], is_admin: bool) -> bool:
        if row["status"] == "archived":
            return is_admin
        return cls._visible(row, is_admin)

    def get_asset(
        self,
        asset_id: str,
        *,
        is_admin: bool,
        connection: StoreConnection | None = None,
    ) -> dict[str, Any]:
        _require_string(asset_id, "assetId")
        if connection is None:
            with self._reader_connection() as own_connection:
                return self.get_asset(
                    asset_id,
                    is_admin=is_admin,
                    connection=own_connection,
                )
        row = connection.execute(
            "SELECT * FROM assets WHERE asset_id = %s", (asset_id,)
        ).fetchone()
        if row is None or not self._asset_visible(row, is_admin):
            raise SemanticError(
                "asset_not_found", "Semantic asset was not found.", status=404
            )
        return self._asset_from_row(row)

    def list_assets(
        self,
        *,
        is_admin: bool,
        connection: StoreConnection | None = None,
        after_asset_id: str | None = None,
        fetch_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        fetch_limit = self._validated_fetch_limit(fetch_limit)
        if after_asset_id is not None:
            _require_string(after_asset_id, "afterAssetId")
        if connection is None:
            with self._reader_connection() as own_connection:
                return self.list_assets(
                    is_admin=is_admin,
                    connection=own_connection,
                    after_asset_id=after_asset_id,
                    fetch_limit=fetch_limit,
                )
        clauses = ["status = 'ready'"]
        values: list[Any] = []
        if not is_admin:
            clauses.append("visibility = 'inspect'")
        if after_asset_id is not None:
            clauses.append("asset_id > %s")
            values.append(after_asset_id)
        statement = (
            f"SELECT * FROM assets WHERE {' AND '.join(clauses)} "
            "ORDER BY asset_id"
        )
        if fetch_limit is not None:
            statement += " LIMIT %s"
            values.append(fetch_limit)
        rows = connection.execute(statement, values).fetchall()
        return [self._asset_from_row(row) for row in rows]

    def search_assets(
        self,
        query: str,
        *,
        limit: int | None,
        is_admin: bool,
        connection: StoreConnection | None = None,
        after_asset_id: str | None = None,
        fetch_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or len(query) > 256:
            raise SemanticError(
                "invalid_request", "q must be a string of at most 256 characters."
            )
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise SemanticError(
                "invalid_request", "limit must be an integer between 1 and 100."
            )
        fetch_limit = self._validated_fetch_limit(fetch_limit)
        if limit is not None and fetch_limit is not None:
            raise SemanticError(
                "invalid_request", "limit and fetch limit cannot both be supplied."
            )
        if after_asset_id is not None:
            _require_string(after_asset_id, "afterAssetId")
        if connection is None:
            with self._reader_connection() as own_connection:
                return self.search_assets(
                    query,
                    limit=limit,
                    is_admin=is_admin,
                    connection=own_connection,
                    after_asset_id=after_asset_id,
                    fetch_limit=fetch_limit,
                )
        # lower() on both sides, not casefold().  The haystack is folded by
        # PostgreSQL and PostgreSQL has no casefold, so folding the needle in
        # Python with str.casefold would leave the two disagreeing: casefold
        # maps "STRASSE" and "Straße" onto the same string and lower()
        # does not.
        needle = query.lower().strip()
        clauses = ["status = 'ready'"]
        values: list[Any] = []
        if not is_admin:
            clauses.append("visibility = 'inspect'")
        if after_asset_id is not None:
            clauses.append("asset_id > %s")
            values.append(after_asset_id)
        if needle:
            clauses.append(
                "position(%s in lower(asset_id || ' ' || generated_json "
                "|| ' ' || curated_json)) > 0"
            )
            values.append(needle)
        effective_limit = fetch_limit if fetch_limit is not None else limit
        statement = (
            f"SELECT * FROM assets WHERE {' AND '.join(clauses)} "
            "ORDER BY asset_id"
        )
        if effective_limit is not None:
            statement += " LIMIT %s"
            values.append(effective_limit)
        rows = connection.execute(statement, values).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            asset = self._asset_from_row(row)
            generated = asset["generated"]
            curated = asset["curated"]
            results.append(
                {
                    "id": asset["id"],
                    "version": asset["version"],
                    "generation": asset["generation"],
                    "status": asset["status"],
                    "visibility": asset["visibility"],
                    "name": curated.get("displayName")
                    or generated.get("name")
                    or asset["id"],
                    "description": curated.get("description")
                    or generated.get("description"),
                    "score": 1.0,
                    "catalogRevision": asset["catalogRevision"],
                }
            )
        return results

    def asset_history(
        self,
        asset_id: str,
        *,
        is_admin: bool,
        connection: StoreConnection | None = None,
        after_history_id: int | None = None,
        fetch_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        fetch_limit = self._validated_fetch_limit(fetch_limit)
        if (
            after_history_id is not None
            and (
                isinstance(after_history_id, bool)
                or not isinstance(after_history_id, int)
                or after_history_id < 1
            )
        ):
            raise SemanticError(
                "invalid_request", "after history ID must be a positive integer."
            )
        if connection is None:
            with self.read_snapshot() as (own_connection, _revision):
                return self.asset_history(
                    asset_id,
                    is_admin=is_admin,
                    connection=own_connection,
                    after_history_id=after_history_id,
                    fetch_limit=fetch_limit,
                )
        self.get_asset(
            asset_id,
            is_admin=is_admin,
            connection=connection,
        )
        clauses = ["asset_id = %s"]
        values: list[Any] = [asset_id]
        if after_history_id is not None:
            clauses.append("history_id > %s")
            values.append(after_history_id)
        statement = f"""
            SELECT history_id, version, generation, catalog_revision, change_type,
                   event_id, proposal_id, actor, snapshot_json, changed_at
              FROM asset_history
             WHERE {' AND '.join(clauses)}
             ORDER BY history_id
            """
        if fetch_limit is not None:
            statement += " LIMIT %s"
            values.append(fetch_limit)
        rows = connection.execute(statement, values).fetchall()
        return [
            {
                **(
                    {"_historyId": row["history_id"]}
                    if fetch_limit is not None
                    else {}
                ),
                "version": row["version"],
                "generation": row["generation"],
                "catalogRevision": row["catalog_revision"],
                "changeType": row["change_type"],
                "eventId": row["event_id"],
                "proposalId": row["proposal_id"],
                "actor": row["actor"],
                "asset": json.loads(row["snapshot_json"]),
                "changedAt": row["changed_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _validate_event(event: dict[str, Any]) -> dict[str, Any]:
        _closed_object(
            event,
            {
                "eventId",
                "assetId",
                "type",
                "generation",
                "generated",
                "visibility",
                "actor",
                "payloadHash",
                "predecessorAssetId",
            },
            "event",
        )
        event_id = _require_string(event.get("eventId"), "eventId")
        asset_id = _require_string(event.get("assetId"), "assetId")
        event_type = event.get("type")
        if event_type not in {"register", "replace", "refresh", "archive"}:
            raise SemanticError(
                "invalid_request",
                "type must be register, replace, refresh, or archive.",
            )
        generation = _require_positive_int(event.get("generation"), "generation")
        generated = event.get("generated")
        if event_type != "archive":
            generated = _require_object(generated, "generated")
        elif generated is not None:
            generated = _require_object(generated, "generated")
        visibility = event.get("visibility")
        if visibility is not None and visibility not in {"inspect", "admin"}:
            raise SemanticError(
                "invalid_request", "visibility must be inspect or admin."
            )
        actor = event.get("actor", "system")
        _require_string(actor, "actor", maximum=256)
        predecessor_asset_id = None
        if "predecessorAssetId" in event:
            predecessor_asset_id = _require_string(
                event["predecessorAssetId"],
                "predecessorAssetId",
            )
            if event_type != "register":
                raise SemanticError(
                    "invalid_request",
                    "predecessorAssetId is accepted only for register events.",
                )
            if predecessor_asset_id == asset_id:
                raise SemanticError(
                    "invalid_request",
                    "predecessorAssetId must differ from assetId.",
                )

        canonical_event = {
            "eventId": event_id,
            "assetId": asset_id,
            "type": event_type,
            "generation": generation,
            "actor": actor,
        }
        if visibility is not None:
            canonical_event["visibility"] = visibility
        if generated is not None:
            canonical_event["generated"] = generated
        if predecessor_asset_id is not None:
            canonical_event["predecessorAssetId"] = predecessor_asset_id
        payload_hash = sha256_json(canonical_event)
        supplied_hash = event.get("payloadHash")
        if supplied_hash is not None:
            if not isinstance(supplied_hash, str) or not _SHA256_RE.fullmatch(
                supplied_hash
            ):
                raise SemanticError(
                    "invalid_request",
                    "payloadHash must be a lowercase SHA-256 hexadecimal string.",
                )
            if supplied_hash != payload_hash:
                raise SemanticError(
                    "payload_hash_mismatch",
                    "payloadHash does not match the canonical event payload.",
                    status=409,
                    details={"computedPayloadHash": payload_hash},
                )
        canonical_event["payloadHash"] = payload_hash
        return canonical_event

    @staticmethod
    def _prepare_fields(
        generated: dict[str, Any],
        old_generated: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        prepared = copy.deepcopy(generated)
        fields = prepared.get("fields")
        if fields is None:
            return prepared, []
        if not isinstance(fields, list):
            raise SemanticError("invalid_request", "generated.fields must be an array.")

        old_fields_by_name: dict[str, dict[str, Any]] = {}
        if old_generated is not None:
            old_fields = old_generated.get("fields", [])
            if isinstance(old_fields, list):
                old_fields_by_name = {
                    field["name"]: field
                    for field in old_fields
                    if isinstance(field, dict)
                    and isinstance(field.get("name"), str)
                    and isinstance(field.get("id"), str)
                }

        names: set[str] = set()
        output: list[dict[str, Any]] = []
        for index, raw_field in enumerate(fields):
            if not isinstance(raw_field, dict):
                raise SemanticError(
                    "invalid_request",
                    f"generated.fields[{index}] must be an object.",
                )
            field = copy.deepcopy(raw_field)
            name = _require_string(
                field.get("name"), f"generated.fields[{index}].name", maximum=256
            )
            if name in names:
                raise SemanticError(
                    "invalid_request",
                    "generated.fields names must be unique.",
                    details={"name": name},
                )
            names.add(name)
            previous = old_fields_by_name.get(name)
            field["id"] = (
                previous["id"]
                if previous is not None
                else f"field:{uuid.uuid4()}"
            )
            output.append(field)
        prepared["fields"] = output
        removed = [
            field
            for name, field in old_fields_by_name.items()
            if name not in names
        ]
        return prepared, removed

    @staticmethod
    def _validate_curated_fields(
        curated: dict[str, Any],
        generated: dict[str, Any],
    ) -> None:
        if "fields" not in curated:
            return
        annotations = curated.get("fields")
        if not isinstance(annotations, dict):
            raise SemanticError(
                "invalid_curated_fields",
                "curated.fields must be an object keyed by stable field ID.",
                status=422,
            )
        generated_fields = generated.get("fields", [])
        valid_ids = {
            field["id"]
            for field in generated_fields
            if isinstance(field, dict)
            and isinstance(field.get("id"), str)
            and field["id"]
        } if isinstance(generated_fields, list) else set()
        try:
            total_bytes = len(canonical_json(annotations).encode("utf-8"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise SemanticError(
                "invalid_curated_fields",
                "curated.fields contains an invalid annotation.",
                status=422,
            ) from exc
        if total_bytes > MAX_CURATED_FIELDS_BYTES:
            raise SemanticError(
                "invalid_curated_fields",
                "curated.fields exceeds the 1 MiB annotation limit.",
                status=422,
            )
        for field_id, annotation in annotations.items():
            if field_id not in valid_ids:
                raise SemanticError(
                    "invalid_curated_fields",
                    "curated.fields references a field that is not active.",
                    status=422,
                    details={"fieldId": field_id},
                )
            if (
                not isinstance(annotation, dict)
                or len(annotation) > MAX_FIELD_ANNOTATION_PROPERTIES
            ):
                raise SemanticError(
                    "invalid_curated_fields",
                    "Each curated field annotation must be an object with "
                    f"at most {MAX_FIELD_ANNOTATION_PROPERTIES} properties.",
                    status=422,
                    details={"fieldId": field_id},
                )
            if (
                len(canonical_json(annotation).encode("utf-8"))
                > MAX_FIELD_ANNOTATION_BYTES
            ):
                raise SemanticError(
                    "invalid_curated_fields",
                    "A curated field annotation exceeds the 16 KiB limit.",
                    status=422,
                    details={"fieldId": field_id},
                )

    def apply_event(self, event: dict[str, Any]) -> dict[str, Any]:
        validated = self._validate_event(_require_object(event, "event"))
        now = self.clock()
        response: dict[str, Any]
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                # Store-wide, because this is the one writer that can create
                # an asset row: there is nothing to lock FOR UPDATE until it
                # has decided whether the asset exists, and the same is true
                # of the processed_events row that makes a replay idempotent.
                # Without it two deliveries of one event would both find no
                # prior row and the loser would fail on the primary key
                # instead of returning the recorded response.
                self._lock_store(connection)
                prior_event = connection.execute(
                    "SELECT * FROM processed_events WHERE event_id = %s",
                    (validated["eventId"],),
                ).fetchone()
                if prior_event is not None:
                    if (
                        prior_event["payload_hash"] != validated["payloadHash"]
                        or prior_event["generation"] != validated["generation"]
                    ):
                        raise SemanticError(
                            "event_conflict",
                            "eventId has already been used for a different event.",
                            status=409,
                        )
                    loaded_response = json.loads(prior_event["response_json"])
                    if not isinstance(loaded_response, dict):
                        raise RuntimeError("stored event response is not an object")
                    response = loaded_response
                    response["event"]["idempotent"] = True
                    connection.execute("COMMIT")
                    return response

                row = connection.execute(
                    "SELECT * FROM assets WHERE asset_id = %s",
                    (validated["assetId"],),
                ).fetchone()
                event_type = validated["type"]
                if event_type == "register":
                    if row is not None:
                        raise SemanticError(
                            "asset_exists",
                            "Semantic asset already exists.",
                            status=409,
                        )
                    predecessor_asset_id = validated.get(
                        "predecessorAssetId"
                    )
                    predecessor = None
                    if predecessor_asset_id is not None:
                        predecessor = connection.execute(
                            "SELECT * FROM assets WHERE asset_id = %s",
                            (predecessor_asset_id,),
                        ).fetchone()
                        if predecessor is None:
                            raise SemanticError(
                                "predecessor_not_found",
                                "Semantic predecessor asset was not found.",
                                status=404,
                            )
                        if predecessor["status"] != "archived":
                            raise SemanticError(
                                "predecessor_not_archived",
                                "Semantic predecessor asset must be archived.",
                                status=409,
                            )
                        predecessor_generated = json.loads(
                            predecessor["generated_json"]
                        )
                        incoming_binding = validated["generated"].get(
                            "binding"
                        )
                        predecessor_binding = predecessor_generated.get(
                            "binding"
                        )
                        if (
                            not isinstance(
                                validated["generated"].get("name"),
                                str,
                            )
                            or not validated["generated"]["name"]
                            or validated["generated"]["name"]
                            != predecessor_generated.get("name")
                            or not isinstance(incoming_binding, dict)
                            or not isinstance(predecessor_binding, dict)
                            or canonical_json(incoming_binding)
                            != canonical_json(predecessor_binding)
                        ):
                            raise SemanticError(
                                "predecessor_binding_mismatch",
                                "Semantic predecessor binding and name must "
                                "match the registering asset.",
                                status=409,
                            )
                        old_generated = predecessor_generated
                        curated = json.loads(
                            predecessor["curated_json"]
                        )
                        orphans = json.loads(
                            predecessor["orphans_json"]
                        )
                    else:
                        old_generated = None
                        curated = {}
                        orphans = []
                    version = 1
                    created_at = now
                    visibility = (
                        validated.get(
                            "visibility",
                            predecessor["visibility"],
                        )
                        if predecessor is not None
                        else validated.get("visibility", "inspect")
                    )
                else:
                    if row is None:
                        raise SemanticError(
                            "asset_not_found",
                            "Semantic asset was not found.",
                            status=404,
                        )
                    predecessor_asset_id = row["predecessor_asset_id"]
                    if row["status"] == "archived":
                        raise SemanticError(
                            "asset_archived",
                            "Archived semantic assets cannot receive source events.",
                            status=409,
                        )
                    if validated["generation"] <= row["generation"]:
                        raise SemanticError(
                            "stale_generation",
                            "Event generation must be newer than the asset generation.",
                            status=409,
                            details={"currentGeneration": row["generation"]},
                        )
                    old_generated = json.loads(row["generated_json"])
                    curated = json.loads(row["curated_json"])
                    orphans = json.loads(row["orphans_json"])
                    version = row["version"] + 1
                    created_at = row["created_at"]
                    visibility = validated.get("visibility", row["visibility"])

                supplied_generated = validated.get("generated")
                if supplied_generated is None:
                    prepared_generated = copy.deepcopy(old_generated or {})
                    removed_fields: list[dict[str, Any]] = []
                else:
                    prepared_generated, removed_fields = self._prepare_fields(
                        supplied_generated, old_generated
                    )

                curated_fields = curated.get("fields")
                if isinstance(curated_fields, dict):
                    for field in removed_fields:
                        field_id = field["id"]
                        if field_id in curated_fields:
                            orphans.append(
                                {
                                    "fieldId": field_id,
                                    "name": field["name"],
                                    "annotation": curated_fields.pop(field_id),
                                    "removedAtGeneration": validated["generation"],
                                }
                            )

                self._validate_curated_fields(
                    curated,
                    prepared_generated,
                )
                status = "archived" if event_type == "archive" else "ready"
                if status == "archived":
                    visibility = "admin"
                archived_at = now if status == "archived" else None
                revision = self._next_catalog_revision(connection)
                values = (
                    version,
                    validated["generation"],
                    status,
                    visibility,
                    canonical_json(prepared_generated),
                    canonical_json(curated),
                    canonical_json(orphans),
                    revision,
                    now,
                    archived_at,
                    validated["assetId"],
                )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO assets(
                            asset_id, version, generation, status, visibility,
                            generated_json, curated_json, orphans_json,
                            catalog_revision, created_at, updated_at, archived_at,
                            predecessor_asset_id
                        ) VALUES(
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            validated["assetId"],
                            version,
                            validated["generation"],
                            status,
                            visibility,
                            canonical_json(prepared_generated),
                            canonical_json(curated),
                            canonical_json(orphans),
                            revision,
                            created_at,
                            now,
                            archived_at,
                            predecessor_asset_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE assets
                           SET version = %s, generation = %s, status = %s,
                               visibility = %s, generated_json = %s,
                               curated_json = %s, orphans_json = %s,
                               catalog_revision = %s, updated_at = %s,
                               archived_at = %s
                         WHERE asset_id = %s
                        """,
                        values,
                    )

                updated_row = connection.execute(
                    "SELECT * FROM assets WHERE asset_id = %s",
                    (validated["assetId"],),
                ).fetchone()
                asset = self._asset_from_row(updated_row)
                connection.execute(
                    """
                    INSERT INTO asset_history(
                        asset_id, version, generation, catalog_revision,
                        change_type, event_id, proposal_id, actor,
                        snapshot_json, changed_at
                    ) VALUES(%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s)
                    """,
                    (
                        asset["id"],
                        asset["version"],
                        asset["generation"],
                        revision,
                        event_type,
                        validated["eventId"],
                        validated["actor"],
                        canonical_json(asset),
                        now,
                    ),
                )
                response = {
                    "event": {
                        "eventId": validated["eventId"],
                        "idempotent": False,
                        "payloadHash": validated["payloadHash"],
                    },
                    "asset": asset,
                    "catalogRevision": revision,
                }
                connection.execute(
                    """
                    INSERT INTO processed_events(
                        event_id, payload_hash, generation,
                        response_json, processed_at
                    ) VALUES(%s, %s, %s, %s, %s)
                    """,
                    (
                        validated["eventId"],
                        validated["payloadHash"],
                        validated["generation"],
                        canonical_json(response),
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return response
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _decode_pointer(path: Any) -> list[str]:
        if path == "/curated":
            return []
        if not isinstance(path, str) or not path.startswith("/curated/"):
            raise SemanticError(
                "invalid_operation",
                "Operation path must select a value below /curated.",
            )
        raw_parts = path.split("/")[2:]
        parts: list[str] = []
        for raw in raw_parts:
            if not raw:
                raise SemanticError(
                    "invalid_operation", "Operation paths cannot contain empty keys."
                )
            index = 0
            decoded = ""
            while index < len(raw):
                if raw[index] != "~":
                    decoded += raw[index]
                    index += 1
                elif raw[index : index + 2] == "~0":
                    decoded += "~"
                    index += 2
                elif raw[index : index + 2] == "~1":
                    decoded += "/"
                    index += 2
                else:
                    raise SemanticError(
                        "invalid_operation",
                        "Operation path contains an invalid JSON Pointer escape.",
                    )
            parts.append(decoded)
        return parts

    @classmethod
    def _validate_operations(cls, operations: Any) -> list[dict[str, Any]]:
        if (
            not isinstance(operations, list)
            or not operations
            or len(operations) > MAX_OPERATIONS
        ):
            raise SemanticError(
                "invalid_request",
                f"operations must contain between 1 and {MAX_OPERATIONS} items.",
            )
        normalized: list[dict[str, Any]] = []
        paths: set[str] = set()
        for index, raw in enumerate(operations):
            operation = _require_object(raw, f"operations[{index}]")
            op = operation.get("op")
            if op == "set":
                _closed_object(operation, {"op", "path", "value"}, f"operations[{index}]")
                if "value" not in operation:
                    raise SemanticError(
                        "invalid_operation", "set operations require value."
                    )
            elif op == "unset":
                _closed_object(operation, {"op", "path"}, f"operations[{index}]")
            else:
                raise SemanticError(
                    "invalid_operation", "Operation op must be set or unset."
                )
            path = operation.get("path")
            if not isinstance(path, str):
                raise SemanticError(
                    "invalid_operation", "Operation path must be a string."
                )
            parts = cls._decode_pointer(path)
            if not parts and op == "unset":
                raise SemanticError(
                    "invalid_operation",
                    "The curated root can be replaced but not unset.",
                )
            if not parts and not isinstance(operation.get("value"), dict):
                raise SemanticError(
                    "invalid_operation",
                    "Setting /curated requires an object value.",
                )
            if path in paths:
                raise SemanticError(
                    "invalid_operation",
                    "A proposal cannot operate on the same path more than once.",
                    details={"path": path},
                )
            paths.add(path)
            normalized.append(copy.deepcopy(operation))
        return normalized

    @staticmethod
    def _read_path(document: dict[str, Any], parts: list[str]) -> Any:
        current: Any = document
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return _MISSING
            current = current[part]
        return current

    @staticmethod
    def _write_path(
        document: dict[str, Any], parts: list[str], value: Any = _MISSING
    ) -> None:
        current = document
        for part in parts[:-1]:
            child = current.get(part)
            if child is None:
                child = {}
                current[part] = child
            if not isinstance(child, dict):
                raise SemanticError(
                    "invalid_operation",
                    "Operation path traverses a non-object value.",
                )
            current = child
        final = parts[-1]
        if value is _MISSING:
            if final not in current:
                raise SemanticError(
                    "invalid_operation",
                    "unset operation selects a value that does not exist.",
                )
            del current[final]
        else:
            current[final] = copy.deepcopy(value)

    @classmethod
    def _evaluate_operations(
        cls,
        curated: dict[str, Any],
        operations: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        result = copy.deepcopy(curated)
        diff: list[dict[str, Any]] = []
        for operation in operations:
            parts = cls._decode_pointer(operation["path"])
            if not parts:
                before = copy.deepcopy(result)
                result = copy.deepcopy(operation["value"])
                diff.append(
                    {
                        "op": "set",
                        "path": "/curated",
                        "before": {"exists": True, "value": before},
                        "after": {"exists": True, "value": copy.deepcopy(result)},
                    }
                )
                continue
            before = cls._read_path(result, parts)
            before_item: dict[str, Any] = {"exists": before is not _MISSING}
            if before is not _MISSING:
                before_item["value"] = copy.deepcopy(before)
            if operation["op"] == "set":
                cls._write_path(result, parts, operation["value"])
                after_item = {"exists": True, "value": copy.deepcopy(operation["value"])}
            else:
                cls._write_path(result, parts)
                after_item = {"exists": False}
            diff.append(
                {
                    "op": operation["op"],
                    "path": operation["path"],
                    "before": before_item,
                    "after": after_item,
                }
            )
        return result, diff

    def check_proposal(
        self,
        request: dict[str, Any],
        *,
        is_admin: bool,
        connection: StoreConnection | None = None,
    ) -> dict[str, Any]:
        request = _require_object(request, "request")
        _closed_object(
            request,
            {
                "assetId",
                "baseVersion",
                "operations",
                "fingerprint",
                "explanation",
            },
            "request",
        )
        asset_id = _require_string(request.get("assetId"), "assetId")
        base_version = _require_positive_int(request.get("baseVersion"), "baseVersion")
        operations = self._validate_operations(request.get("operations"))
        explanation = request.get("explanation")
        if explanation is not None and (
            not isinstance(explanation, str)
            or not explanation.strip()
            or len(explanation) > 4000
        ):
            raise SemanticError(
                "invalid_request",
                "explanation must be a non-empty string of at most 4000 characters.",
            )
        asset = self.get_asset(
            asset_id,
            is_admin=is_admin,
            connection=connection,
        )
        if asset["status"] == "archived":
            raise SemanticError(
                "asset_archived",
                "Archived semantic assets cannot be edited.",
                status=409,
            )
        if base_version != asset["version"]:
            raise SemanticError(
                "revision_conflict",
                "Asset version changed; create a new proposal check.",
                status=409,
                details={"currentVersion": asset["version"]},
            )
        updated_curated, diff = self._evaluate_operations(
            asset["curated"],
            operations,
        )
        self._validate_curated_fields(
            updated_curated,
            asset["generated"],
        )
        fingerprint_payload = {
            "assetId": asset_id,
            "baseVersion": base_version,
            "operations": operations,
            "diff": diff,
            "explanation": explanation,
        }
        return {
            **fingerprint_payload,
            "fingerprint": sha256_json(fingerprint_payload),
        }

    def create_proposal(
        self,
        request: dict[str, Any],
        *,
        actor: str,
        is_admin: bool,
    ) -> dict[str, Any]:
        request = _require_object(request, "request")
        _closed_object(
            request,
            {
                "assetId",
                "baseVersion",
                "operations",
                "fingerprint",
                "explanation",
            },
            "request",
        )
        supplied_fingerprint = request.get("fingerprint")
        if not isinstance(supplied_fingerprint, str) or not _SHA256_RE.fullmatch(
            supplied_fingerprint
        ):
            raise SemanticError(
                "invalid_request",
                "fingerprint must be a lowercase SHA-256 hexadecimal string.",
            )
        check = self.check_proposal(request, is_admin=is_admin)
        if supplied_fingerprint != check["fingerprint"]:
            raise SemanticError(
                "fingerprint_mismatch",
                "Proposal fingerprint does not match the checked operation.",
                status=409,
                details={"expectedFingerprint": check["fingerprint"]},
            )
        now = self.clock()
        proposal_id = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                # FOR UPDATE on the asset alone, not the store-wide lock: this
                # writer publishes no catalog revision, and all it needs is
                # that the version it validated against cannot move between
                # the check below and the insert.
                row = connection.execute(
                    """
                    SELECT version, status, visibility
                    FROM assets
                    WHERE asset_id = %s
                    FOR UPDATE
                    """,
                    (check["assetId"],),
                ).fetchone()
                if row is None or not self._visible(row, is_admin):
                    raise SemanticError(
                        "asset_not_found",
                        "Semantic asset was not found.",
                        status=404,
                    )
                if (
                    row["status"] != "ready"
                    or row["version"] != check["baseVersion"]
                ):
                    raise SemanticError(
                        "revision_conflict",
                        "Asset version changed; create a new proposal check.",
                        status=409,
                        details={
                            "currentVersion": row["version"]
                        },
                    )
                connection.execute(
                    """
                    INSERT INTO proposals(
                        proposal_id, state, asset_id, base_version,
                        operations_json, fingerprint, diff_json,
                        explanation, actor, reason, created_at, updated_at,
                        applied_version
                    ) VALUES(
                        %s, 'pending', %s, %s, %s, %s, %s, %s, %s, NULL,
                        %s, %s, NULL
                    )
                    """,
                    (
                        proposal_id,
                        check["assetId"],
                        check["baseVersion"],
                        canonical_json(check["operations"]),
                        check["fingerprint"],
                        canonical_json(check["diff"]),
                        check["explanation"],
                        actor,
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_proposal(proposal_id, is_admin=is_admin)

    @staticmethod
    def _proposal_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["proposal_id"],
            "state": row["state"],
            "assetId": row["asset_id"],
            "baseVersion": row["base_version"],
            "operations": json.loads(row["operations_json"]),
            "fingerprint": row["fingerprint"],
            "diff": json.loads(row["diff_json"]),
            "explanation": row["explanation"],
            "actor": row["actor"],
            "reason": row["reason"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "appliedVersion": row["applied_version"],
            "decidedBy": row["decided_by"],
            "decidedAt": row["decided_at"],
        }

    def get_proposal(
        self,
        proposal_id: str,
        *,
        is_admin: bool,
        connection: StoreConnection | None = None,
    ) -> dict[str, Any]:
        _require_string(proposal_id, "proposalId")
        if connection is None:
            with self._reader_connection() as own_connection:
                return self.get_proposal(
                    proposal_id,
                    is_admin=is_admin,
                    connection=own_connection,
                )
        row = connection.execute(
            """
            SELECT p.*, a.visibility
              FROM proposals p
              JOIN assets a ON a.asset_id = p.asset_id
             WHERE p.proposal_id = %s
            """,
            (proposal_id,),
        ).fetchone()
        if row is None or not self._visible(row, is_admin):
            raise SemanticError(
                "proposal_not_found",
                "Semantic proposal was not found.",
                status=404,
            )
        return self._proposal_from_row(row)

    def list_proposals(
        self,
        *,
        state: str | None,
        asset_id: str | None,
        is_admin: bool,
        connection: StoreConnection | None = None,
        after: tuple[str, str] | None = None,
        fetch_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if state is not None and state not in {"pending", "applied", "declined"}:
            raise SemanticError(
                "invalid_request", "state must be pending, applied, or declined."
            )
        if asset_id is not None:
            _require_string(asset_id, "assetId")
        fetch_limit = self._validated_fetch_limit(fetch_limit)
        if after is not None:
            if not isinstance(after, tuple) or len(after) != 2:
                raise SemanticError(
                    "invalid_request", "proposal page position is invalid."
                )
            _require_string(after[0], "afterCreatedAt")
            _require_string(after[1], "afterProposalId")
        clauses: list[str] = []
        values: list[Any] = []
        if state is not None:
            clauses.append("p.state = %s")
            values.append(state)
        if asset_id is not None:
            clauses.append("p.asset_id = %s")
            values.append(asset_id)
        if not is_admin:
            clauses.append("a.visibility = 'inspect'")
        if after is not None:
            clauses.append(
                "(p.created_at > %s OR "
                "(p.created_at = %s AND p.proposal_id > %s))"
            )
            values.extend((after[0], after[0], after[1]))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if connection is None:
            with self._reader_connection() as own_connection:
                return self.list_proposals(
                    state=state,
                    asset_id=asset_id,
                    is_admin=is_admin,
                    connection=own_connection,
                    after=after,
                    fetch_limit=fetch_limit,
                )
        statement = f"""
            SELECT p.*, a.visibility
              FROM proposals p
              JOIN assets a ON a.asset_id = p.asset_id
              {where}
             ORDER BY p.created_at, p.proposal_id
            """
        if fetch_limit is not None:
            statement += " LIMIT %s"
            values.append(fetch_limit)
        rows = connection.execute(statement, values).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def apply_proposal(
        self,
        proposal_id: str,
        *,
        actor: str,
        is_admin: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        # Resolve visibility before taking the write lock.
        self.get_proposal(proposal_id, is_admin=is_admin)
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                # Store-wide, because this publishes a catalog revision, plus
                # FOR UPDATE on the proposal and on the asset it changes.  The
                # row lock on the proposal is what excludes decline_proposal,
                # which takes no store-wide lock: without it a decline could
                # commit between the state check here and the update below and
                # the proposal would end up both declined and applied.
                self._lock_store(connection)
                proposal_row = connection.execute(
                    "SELECT * FROM proposals WHERE proposal_id = %s"
                    " FOR UPDATE",
                    (proposal_id,),
                ).fetchone()
                if proposal_row["state"] != "pending":
                    raise SemanticError(
                        "proposal_not_pending",
                        "Only pending proposals can be applied.",
                        status=409,
                    )
                asset_row = connection.execute(
                    "SELECT * FROM assets WHERE asset_id = %s FOR UPDATE",
                    (proposal_row["asset_id"],),
                ).fetchone()
                if asset_row is None or not self._visible(
                    asset_row,
                    is_admin,
                ):
                    raise SemanticError(
                        "proposal_not_found",
                        "Semantic proposal was not found.",
                        status=404,
                    )
                if (
                    asset_row["status"] != "ready"
                    or asset_row["version"] != proposal_row["base_version"]
                ):
                    raise SemanticError(
                        "revision_conflict",
                        "Asset version changed; create a new proposal.",
                        status=409,
                        details={"currentVersion": asset_row["version"]},
                    )
                operations = json.loads(proposal_row["operations_json"])
                curated = json.loads(asset_row["curated_json"])
                updated_curated, diff = self._evaluate_operations(curated, operations)
                self._validate_curated_fields(
                    updated_curated,
                    json.loads(asset_row["generated_json"]),
                )
                if canonical_json(diff) != proposal_row["diff_json"]:
                    raise SemanticError(
                        "proposal_corrupt",
                        "Stored proposal evidence no longer matches its operation.",
                        status=409,
                    )
                version = asset_row["version"] + 1
                revision = self._next_catalog_revision(connection)
                connection.execute(
                    """
                    UPDATE assets
                       SET version = %s, curated_json = %s,
                           catalog_revision = %s, updated_at = %s
                     WHERE asset_id = %s
                    """,
                    (
                        version,
                        canonical_json(updated_curated),
                        revision,
                        now,
                        asset_row["asset_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE proposals
                       SET state = 'applied', updated_at = %s,
                           applied_version = %s, decided_by = %s,
                           decided_at = %s
                     WHERE proposal_id = %s
                    """,
                    (now, version, actor, now, proposal_id),
                )
                updated_proposal_row = connection.execute(
                    "SELECT * FROM proposals WHERE proposal_id = %s",
                    (proposal_id,),
                ).fetchone()
                proposal = self._proposal_from_row(updated_proposal_row)
                updated_row = connection.execute(
                    "SELECT * FROM assets WHERE asset_id = %s",
                    (asset_row["asset_id"],),
                ).fetchone()
                asset = self._asset_from_row(updated_row)
                connection.execute(
                    """
                    INSERT INTO asset_history(
                        asset_id, version, generation, catalog_revision,
                        change_type, event_id, proposal_id, actor,
                        snapshot_json, changed_at
                    ) VALUES(%s, %s, %s, %s, 'curated', NULL, %s, %s, %s, %s)
                    """,
                    (
                        asset["id"],
                        asset["version"],
                        asset["generation"],
                        revision,
                        proposal_id,
                        actor,
                        canonical_json(asset),
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return proposal, asset, revision

    def decline_proposal(
        self,
        proposal_id: str,
        *,
        actor: str,
        reason: str | None,
        is_admin: bool,
    ) -> dict[str, Any]:
        self.get_proposal(proposal_id, is_admin=is_admin)
        if reason is not None and (
            not isinstance(reason, str) or not reason or len(reason) > 2000
        ):
            raise SemanticError(
                "invalid_request",
                "reason must be a non-empty string of at most 2000 characters.",
            )
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                # FOR UPDATE OF p, so only the proposal row is locked and the
                # joined asset is left alone.  This writer publishes no
                # catalog revision, so it needs no store-wide lock; the row
                # lock is what makes it exclusive with apply_proposal.
                row = connection.execute(
                    """
                    SELECT p.state, a.visibility
                    FROM proposals AS p
                    JOIN assets AS a ON a.asset_id = p.asset_id
                    WHERE p.proposal_id = %s
                    FOR UPDATE OF p
                    """,
                    (proposal_id,),
                ).fetchone()
                if row is None or not self._visible(row, is_admin):
                    raise SemanticError(
                        "proposal_not_found",
                        "Semantic proposal was not found.",
                        status=404,
                    )
                if row["state"] != "pending":
                    raise SemanticError(
                        "proposal_not_pending",
                        "Only pending proposals can be declined.",
                        status=409,
                    )
                connection.execute(
                    """
                    UPDATE proposals
                       SET state = 'declined', reason = %s, updated_at = %s,
                           decided_by = %s, decided_at = %s
                     WHERE proposal_id = %s
                    """,
                    (reason, now, actor, now, proposal_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_proposal(proposal_id, is_admin=is_admin)

    def derived_profiles(
        self,
        *,
        is_admin: bool,
        connection: StoreConnection | None = None,
        after_asset_id: str | None = None,
        fetch_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        fetch_limit = self._validated_fetch_limit(fetch_limit)
        if after_asset_id is not None:
            _require_string(after_asset_id, "afterAssetId")
        if connection is None:
            with self._reader_connection() as own_connection:
                return self.derived_profiles(
                    is_admin=is_admin,
                    connection=own_connection,
                    after_asset_id=after_asset_id,
                    fetch_limit=fetch_limit,
                )
        clauses = [
            "status = 'ready'",
            "(generated_json::jsonb -> 'binding' ->> 'schema' "
            "= 'derived_layers' OR position('derived' in "
            "lower(generated_json::jsonb ->> 'kind')) > 0)",
        ]
        values: list[Any] = []
        if not is_admin:
            clauses.append("visibility = 'inspect'")
        if after_asset_id is not None:
            clauses.append("asset_id > %s")
            values.append(after_asset_id)
        statement = (
            f"SELECT * FROM assets WHERE {' AND '.join(clauses)} "
            "ORDER BY asset_id"
        )
        if fetch_limit is not None:
            statement += " LIMIT %s"
            values.append(fetch_limit)
        rows = connection.execute(statement, values).fetchall()
        return [self._asset_from_row(row) for row in rows]

    @staticmethod
    def derived_name(asset: dict[str, Any]) -> str | None:
        generated = asset["generated"]
        name = generated.get("name")
        if isinstance(name, str):
            return name
        binding = generated.get("binding")
        if isinstance(binding, dict):
            for key in ("relation", "table", "name"):
                value = binding.get(key)
                if isinstance(value, str):
                    return value
        return None

    def get_derived_profile(
        self,
        name: str,
        *,
        is_admin: bool,
        connection: StoreConnection | None = None,
    ) -> dict[str, Any]:
        _require_string(name, "name")
        for asset in self.derived_profiles(
            is_admin=is_admin,
            connection=connection,
        ):
            if self.derived_name(asset) == name or asset["id"] == name:
                return asset
        raise SemanticError(
            "derived_profile_not_found",
            "Derived semantic profile was not found.",
            status=404,
        )

    def status(self) -> dict[str, Any]:
        settings = self.database_settings()
        return {
            "ok": True,
            "schemaVersion": settings["schemaVersion"],
            "catalogRevision": self.catalog_revision(),
            "capabilities": {
                "catalog": True,
                "search": True,
                "derivedProfiles": True,
                "proposals": True,
                "generatedEvents": [
                    "register",
                    "replace",
                    "refresh",
                    "archive",
                ],
                "curatedProposals": True,
            },
        }
