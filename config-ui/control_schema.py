"""The `control` schema: migration ladder, connect helper, and its DDL.

The configuration service used to keep its authorization state in a JSON
document under `var/control`. This module is the PostgreSQL side that replaced
it, and the storage the mcp-auth component needs for its OAuth records.

Two rules run through the whole schema.

**A one-shot record is never read by `SELECT`.** The conditional
``UPDATE ... WHERE ... AND consumed_at IS NULL ... RETURNING`` *is* the read, so
exactly one caller can win a race, and the loser learns it lost from an empty
result rather than from a second query. Consumed rows are kept rather than
deleted, so a replay is detectably a replay instead of merely unknown.

**Authority carries a recovery epoch -- as storage, not yet as a control.**
Every table whose rows can authorise something has ``recovery_epoch``. The
design is that restoring a snapshot bumps the epoch and every lookup compares
against it, so a restore invalidates what was issued before it without
deleting the audit trail. *None of that is implemented*: nothing writes the
column and nothing reads it, so every row sits at 0 forever. It is reserved
storage so the eventual writer needs no migration, and it must not be mistaken
for a live control -- a restore today invalidates nothing.

No table uses a sequence or an identity column: primary keys are sha256 hashes
of secrets the client already holds. That is partly least-privilege --
``test_database_access_contract`` asserts ``ON SEQUENCES`` appears in no grant --
and partly because a hash key makes the lookup and the uniqueness constraint the
same object.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

SCHEMA = "control"

#: Serialises the whole ladder. Held for the migration transaction only, so a
#: second service starting at the same moment waits and then finds every
#: migration already recorded rather than racing CREATE TABLE.
MIGRATION_LOCK = "mapp-control-schema"

#: Bounds a stuck migration rather than letting a start hang forever. The ladder
#: is small and runs on an empty or nearly empty schema.
MIGRATION_TIMEOUT_MS = 30_000

CONNECT_TIMEOUT_SECONDS = 10


class ControlSchemaUnavailable(RuntimeError):
    """No control database is configured, or it cannot be reached."""


def control_dsn() -> str | None:
    """The DSN for the control schema, or None when none is configured.

    Returning None rather than raising is deliberate: the packaged deployment
    always has one, but external-PostgreSQL deployments have no packaged
    database at all, and the caller decides whether that is fatal.
    """
    return os.environ.get("CONTROL_DATABASE_URL") or None


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Open a control connection with a safe search path.

    ``pg_catalog`` first and the schema second, so an object planted in a
    writable schema cannot shadow a builtin. autocommit is on and transactions
    are opened explicitly, matching semantic_store, because the migration
    ladder needs one transaction spanning several statements while ordinary
    calls want none.
    """
    dsn = dsn or control_dsn()
    if not dsn:
        raise ControlSchemaUnavailable(
            "CONTROL_DATABASE_URL is not set; the control schema is unavailable."
        )
    connection = psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        connection.execute(
            sql.SQL("SET SESSION search_path = pg_catalog, {schema}").format(
                schema=sql.Identifier(SCHEMA)
            )
        )
    except BaseException:
        connection.close()
        raise
    return connection


@contextlib.contextmanager
def transaction(connection: psycopg.Connection) -> Iterator[psycopg.Connection]:
    connection.execute("BEGIN")
    try:
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def migrate(connection: psycopg.Connection) -> list[int]:
    """Apply every unapplied migration. Returns the versions applied.

    One advisory lock and one transaction for the whole ladder: PostgreSQL DDL
    is transactional, so a concurrent start blocks here and then finds the
    ladder complete instead of racing CREATE TABLE.
    """
    applied: list[int] = []
    connection.execute("BEGIN")
    try:
        # set_config, not SET LOCAL: the latter takes no parameter placeholder.
        # `true` scopes it to this transaction, so the timeout does not leak on
        # to a pooled connection's later work.
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(MIGRATION_TIMEOUT_MS),),
        )
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (MIGRATION_LOCK,)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (
                    version bigint PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            ).format(schema=sql.Identifier(SCHEMA))
        )
        done = {
            row["version"]
            for row in connection.execute(
                sql.SQL("SELECT version FROM {schema}.schema_migrations").format(
                    schema=sql.Identifier(SCHEMA)
                )
            )
        }
        for version, migration in sorted(MIGRATIONS.items()):
            if version in done:
                continue
            migration(connection)
            connection.execute(
                sql.SQL("INSERT INTO {schema}.schema_migrations(version) VALUES(%s)").format(
                    schema=sql.Identifier(SCHEMA)
                ),
                (version,),
            )
            applied.append(version)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return applied


def _migration_1(connection: psycopg.Connection) -> None:
    """Platform authorization state, replacing the former JSON document.

    Column names are snake_case here while the JSON document used camelCase;
    the importer maps between them in one place rather than spreading the
    difference through the store.
    """
    connection.execute(
        sql.SQL(
            """
        CREATE TABLE {schema}.metadata (
            key   text PRIMARY KEY,
            value text NOT NULL
        );

        -- Exactly one row, enforced by the primary key rather than by
        -- convention, so a second credential cannot be inserted at all.
        CREATE TABLE {schema}.admin_credential (
            id          smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            encoded     text        NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now()
        );

        -- Browser sessions for the configuration dashboard. Expiry is derived
        -- from created_at and last_used_at exactly as the JSON store derived
        -- it, so the two idle/absolute bounds keep one definition in Python.
        CREATE TABLE {schema}.sessions (
            session_hash   text        PRIMARY KEY,
            csrf_hash      text        NOT NULL,
            created_at     timestamptz NOT NULL,
            last_used_at   timestamptz NOT NULL,
            remote         text,
            recovery_epoch bigint      NOT NULL DEFAULT 0
        );

        CREATE TABLE {schema}.tokens (
            token_hash     text        PRIMARY KEY,
            token_id       text        NOT NULL UNIQUE,
            name           text        NOT NULL,
            -- The case-folded name, computed in Python with str.casefold and
            -- stored, never recomputed in SQL: PostgreSQL lower() is not
            -- casefold ('ß' folds to 'ss' in Python, stays 'ß' in lower()), so
            -- deriving it here would silently reserve a different set of names
            -- than the code checks.
            name_key       text        NOT NULL,
            created_at     timestamptz NOT NULL,
            expires_at     timestamptz,
            last_used_at   timestamptz,
            revoked_at     timestamptz,
            scopes         text[]      NOT NULL,
            recovery_epoch bigint      NOT NULL DEFAULT 0
        );

        -- Unique among LIVE tokens only, which is what the data supports and
        -- what matters operationally. The store's rule is stronger -- a name
        -- stays reserved after revocation -- and that stays in Python, because
        -- real deployments predate it: one holds 278 tokens of which 7 folded
        -- names repeat, 51 sharing `federation-e2e`, all minted before the
        -- harness began appending a random suffix. Every one of those is
        -- revoked, and no two live tokens anywhere share a name, so this index
        -- admits the history while making the invariant that still matters
        -- structural rather than merely intended.
        CREATE UNIQUE INDEX tokens_live_name_idx
 ON {schema}.tokens (name_key) WHERE revoked_at IS NULL;
        CREATE INDEX tokens_name_key_idx
 ON {schema}.tokens (name_key);

        CREATE TABLE {schema}.device_authorizations (
            id_hash        text        PRIMARY KEY,
            user_code      text        NOT NULL,
            device_name    text        NOT NULL,
            scopes         text[]      NOT NULL,
            created_at     timestamptz NOT NULL,
            expires_at     timestamptz NOT NULL,
            remote         text,
            status         text        NOT NULL
                             CHECK (status IN ('pending','approved','consumed','revoked')),
            approved_at    timestamptz,
            consumed_at    timestamptz,
            token_id       text,
            recovery_epoch bigint      NOT NULL DEFAULT 0,
            -- One-shot: a consumed row must say when, and a row that is not
            -- consumed must not pretend to have been.
            CONSTRAINT device_consumed_at_matches_status
                CHECK ((status = 'consumed') = (consumed_at IS NOT NULL))
        );

        CREATE UNIQUE INDEX device_user_code_idx
            ON {schema}.device_authorizations (user_code)
            WHERE status = 'pending';
        """
        ).format(schema=sql.Identifier(SCHEMA))
    )


def _migration_2(connection: psycopg.Connection) -> None:
    """OAuth records for the mcp-auth component.

    Mirrors mcp-auth/stub_store.py, which is the in-memory stand-in this
    replaces. Where the stub deletes a consumed record, this marks it: the
    conditional update is the read, and the surviving row is what makes a
    replay detectable rather than merely unrecognised.
    """
    connection.execute(
        sql.SQL(
            """
        CREATE TABLE {schema}.oauth_clients (
            client_id                 text        PRIMARY KEY,
            name                      text        NOT NULL,
            redirect_uris             text[]      NOT NULL,
            scopes                    text[]      NOT NULL,
            grant_types               text[]      NOT NULL,
            token_endpoint_auth_method text       NOT NULL,
            client_secret_hash        text,
            created_at                timestamptz NOT NULL DEFAULT now(),
            disabled_at               timestamptz
        );

        CREATE TABLE {schema}.oauth_authorization_codes (
            code_hash             text        PRIMARY KEY,
            client_id             text        NOT NULL REFERENCES {schema}.oauth_clients(client_id),
            redirect_uri          text        NOT NULL,
            scope                 text        NOT NULL,
            subject               text        NOT NULL,
            code_challenge        text        NOT NULL,
            code_challenge_method text        NOT NULL DEFAULT 'S256'
                                    CHECK (code_challenge_method = 'S256'),
            created_at            timestamptz NOT NULL DEFAULT now(),
            expires_at            timestamptz NOT NULL,
            consumed_at           timestamptz,
            consumed_by           text,
            recovery_epoch        bigint      NOT NULL DEFAULT 0,
            -- Who consumed it is only meaningful once it has been consumed.
            CONSTRAINT code_consumed_by_requires_consumed_at
                CHECK (consumed_by IS NULL OR consumed_at IS NOT NULL)
        );

        CREATE INDEX oauth_codes_expiry_idx
 ON {schema}.oauth_authorization_codes (expires_at);

        CREATE TABLE {schema}.oauth_tokens (
            token_hash     text        PRIMARY KEY,
            client_id      text        NOT NULL REFERENCES {schema}.oauth_clients(client_id),
            subject        text        NOT NULL,
            scope          text        NOT NULL,
            audience       text        NOT NULL,
            issued_at      timestamptz NOT NULL DEFAULT now(),
            expires_at     timestamptz NOT NULL,
            revoked_at     timestamptz,
            consumed_at    timestamptz,
            single_use     boolean     NOT NULL DEFAULT false,
            recovery_epoch bigint      NOT NULL DEFAULT 0,
            -- Only a single-use token can be consumed; a long-lived one is
            -- revoked instead, and conflating the two would let a replay of a
            -- single-use token look like an ordinary expiry.
            CONSTRAINT token_consumed_requires_single_use
                CHECK (consumed_at IS NULL OR single_use)
        );

        CREATE INDEX oauth_tokens_expiry_idx
 ON {schema}.oauth_tokens (expires_at);

        CREATE TABLE {schema}.oauth_pending_authorizations (
            request_id     text        PRIMARY KEY,
            query          text        NOT NULL,
            client_id      text        NOT NULL REFERENCES {schema}.oauth_clients(client_id),
            redirect_uri   text        NOT NULL,
            scopes         text[]      NOT NULL,
            csrf           text        NOT NULL,
            -- Empty until the consent page is rendered to a signed-in
            -- operator. The consume predicate rejects the empty value, so a
            -- record nobody has been shown cannot be submitted.
            session_hash   text        NOT NULL DEFAULT '',
            source         text        NOT NULL DEFAULT '',
            created_at     timestamptz NOT NULL DEFAULT now(),
            expires_at     timestamptz NOT NULL,
            consumed_at    timestamptz,
            recovery_epoch bigint      NOT NULL DEFAULT 0
        );

        CREATE INDEX oauth_pending_source_idx
            ON {schema}.oauth_pending_authorizations (source)
            WHERE consumed_at IS NULL;
        CREATE INDEX oauth_pending_expiry_idx
 ON {schema}.oauth_pending_authorizations (expires_at);

        CREATE TABLE {schema}.oauth_sessions (
            session_hash   text        PRIMARY KEY,
            subject        text        NOT NULL,
            auth_time      timestamptz NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            expires_at     timestamptz NOT NULL,
            recovery_epoch bigint      NOT NULL DEFAULT 0
        );

        CREATE INDEX oauth_sessions_expiry_idx
 ON {schema}.oauth_sessions (expires_at);
        """
        ).format(schema=sql.Identifier(SCHEMA))
    )


def _migration_3(connection: psycopg.Connection) -> None:
    """Bind an exchanged token to the one operation it was issued for.

    A token B is meant to authorise exactly one allowlisted operation against
    one canonical request, with the configuration API re-checking that binding
    on every call. These columns are the storage for it. The re-check itself is
    M7 and does not exist yet, so today the binding is written and never read
    outside the authorization component -- worth knowing before treating an
    issued token B as narrower than its scope.

    Added as a third migration rather than by editing the second: a database
    that already applied migration 2 would keep reporting it as applied and
    silently never acquire these columns.
    """
    connection.execute(
        sql.SQL(
            """
            ALTER TABLE {schema}.oauth_tokens
                ADD COLUMN operation_id    text,
                ADD COLUMN request_digest  text,
                -- The MCP client the grant belongs to, derived from validated
                -- token-A state and never from exchange input: a caller that
                -- could name its own originating client could borrow another
                -- client's authority.
                ADD COLUMN actor_client_id text,
                -- The broker that performed the exchange, kept distinct from
                -- the actor so an audit can tell "who asked" from "who
                -- brokered".
                ADD COLUMN broker_client_id text,
                -- Both halves of the binding travel together or not at all.
                ADD CONSTRAINT token_operation_binding_is_complete
                    CHECK ((operation_id IS NULL) = (request_digest IS NULL)),
                -- A single-use token must name what it is single-use *for*.
                ADD CONSTRAINT token_single_use_is_bound
                    CHECK (NOT single_use OR operation_id IS NOT NULL)
            """
        ).format(schema=sql.Identifier(SCHEMA))
    )


def _migration_4(connection: psycopg.Connection) -> None:
    """The grant, as a record that can be revoked.

    Until now nothing represented a consent. Tokens carried a `subject` string
    which, after a real authorization-code flow, was the literal "admin" for
    every consent by every client for every scope set -- so two grants were
    indistinguishable and "revoke the grant" had nothing to act on. P3 says the
    actor is the grant rather than a directory user; this is what makes that
    true rather than aspirational.

    It is also what lets revocation mean what the scope document requires:
    revoking a grant invalidates an already-issued token B immediately, instead
    of leaving it live for the rest of its sixty seconds, because introspection
    resolves every token through its grant.
    """
    connection.execute(
        sql.SQL(
            """
            CREATE TABLE {schema}.oauth_grants (
                grant_id       text        PRIMARY KEY,
                client_id      text        NOT NULL
                                 REFERENCES {schema}.oauth_clients(client_id),
                -- Who consented. Today a single shared administrator (P3), so
                -- this is not an identity claim -- it records which operator
                -- session authorised the grant, not who they are.
                subject        text        NOT NULL,
                -- Exactly what was consented to. A token may never widen
                -- beyond this, and it is the set the consent screen displayed.
                scopes         text[]      NOT NULL,
                created_at     timestamptz NOT NULL DEFAULT now(),
                revoked_at     timestamptz,
                revoked_reason text,
                recovery_epoch bigint      NOT NULL DEFAULT 0,
                CONSTRAINT grant_reason_requires_revocation
                    CHECK (revoked_reason IS NULL OR revoked_at IS NOT NULL)
            );

            -- Both indexes are for queries that do not exist yet: "the live
            -- grants of one client" and "the tokens of one grant". Every
            -- lookup the code performs today is by primary key, so neither
            -- has a reader -- they are reserved, like recovery_epoch, and
            -- saying so is better than a comment that describes a mechanism.
            CREATE INDEX oauth_grants_live_idx
                ON {schema}.oauth_grants (client_id) WHERE revoked_at IS NULL;

            -- No separate grant_id column: `subject` already holds the grant
            -- id. authenticate_user returns it off the authorization code and
            -- the token inherits it, so a second column would be the same
            -- value written twice and free to drift. Introspection's grant
            -- lookup is a primary-key read of oauth_grants and cannot use an
            -- index on oauth_tokens at all.
            --
            -- No foreign key either, deliberately. Rows written before grants
            -- existed carry an operator name rather than a grant id, and a
            -- constraint would fail the migration on them. Every path that
            -- reads a credential treats a subject resolving to no grant as
            -- dead -- introspection, the exchange, and both halves of the
            -- token-B verification surface -- so the absence fails closed
            -- without the database enforcing it. mcp-auth/tests/
            -- store_contract.py is what holds that true of both stores.
            CREATE INDEX oauth_tokens_subject_idx ON {schema}.oauth_tokens (subject);
            """
        ).format(schema=sql.Identifier(SCHEMA))
    )


#: Every table the ladder creates, ordered so a plain DELETE or TRUNCATE can
#: walk it front to back: children before the parents they reference.
#:
#: Defined here because six fixtures and a benchmark each had their own copy of
#: this order, and migration 6 broke every one of them at once -- its
#: oauth_refresh_families references oauth_clients, so a list written before it
#: existed deleted the clients first and hit a foreign key. One list, one place
#: to update, and a test below that fails if a migration adds a table and
#: forgets it.
TABLES_IN_DELETE_ORDER = (
    "oauth_refresh_tokens",
    "oauth_refresh_families",
    "oauth_authorization_codes",
    "oauth_pending_authorizations",
    "oauth_tokens",
    "oauth_sessions",
    "oauth_grants",
    "oauth_clients",
    "device_authorizations",
    "tokens",
    "sessions",
    "admin_credential",
    "metadata",
)


def all_tables(connection: psycopg.Connection) -> set[str]:
    """Every table the control schema actually holds, for the drift check."""
    return {
        row["table_name"]
        for row in connection.execute(
            sql.SQL(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = %s"
            ),
            (SCHEMA,),
        ).fetchall()
    }


#: Every table whose rows carry authority and are stamped with the epoch they
#: were minted under. Split by how each is invalidated, because they do not
#: share a column: three record a revocation, the rest are ephemeral and are
#: removed outright.
EPOCH_REVOKED_TABLES = (
    "tokens",
    "oauth_tokens",
    "oauth_grants",
    # The family, not its tokens: the family is the unit of authority, so
    # revoking it kills every token in it, and the tokens are worth keeping
    # as replay evidence rather than sweeping.
    "oauth_refresh_families",
)

#: Sessions, one-shot codes and in-flight authorizations. Nothing here is worth
#: keeping as evidence after a restore, and none of them has a revoked_at to
#: mark. `tokens` is deliberately *not* in this list: a revoked token's
#: case-insensitive name must stay reserved, so it is revoked and kept.
EPOCH_DELETED_TABLES = (
    "sessions",
    "oauth_sessions",
    "device_authorizations",
    "oauth_authorization_codes",
    "oauth_pending_authorizations",
)


#: The epoch-bearing tables as they stood when migration 5 was written. Frozen
#: on purpose -- see the comment in the loop below.
_MIGRATION_5_TABLES = (
    "tokens",
    "oauth_tokens",
    "oauth_grants",
    "sessions",
    "oauth_sessions",
    "device_authorizations",
    "oauth_authorization_codes",
    "oauth_pending_authorizations",
)


def _migration_5(connection: psycopg.Connection) -> None:
    """Make `recovery_epoch` a mechanism instead of reserved storage.

    The columns have existed since migration 1 and nothing ever wrote or read
    them, so a restore reinstated every credential that was valid at snapshot
    time -- including ones revoked since. Phase 1 requires the opposite: "a
    restore advances the recovery epoch and cannot make a pre-restore
    grant/A mapping, refresh family or B usable".

    The cheap way to do that is the point of this migration. An earlier
    analysis costed the epoch as a read-time predicate -- `AND recovery_epoch =
    current` on every credential read, 46 statements across two components --
    and concluded it was not worth it. That was the wrong design. Every read
    already filters on a revocation, so the epoch only has to be applied
    *once, at restore*, by revoking what predates it. Reads never change.

    Which leaves one problem: rows inserted after an advance must carry the new
    epoch, or the next advance would sweep them too. Rather than pass it at
    every INSERT -- which is where the 46 statements came from -- the column
    default calls a function that reads the current value. No application code
    changes at all.
    """
    connection.execute(
        sql.SQL(
            "INSERT INTO {schema}.metadata(key, value) VALUES('recovery_epoch', '0')"
            " ON CONFLICT (key) DO NOTHING"
        ).format(schema=sql.Identifier(SCHEMA))
    )
    # STABLE, not IMMUTABLE: the value changes between statements, and marking
    # it immutable would let the planner fold a stale value into a cached plan.
    # Schema-qualified inside, because the control role's search_path puts
    # pg_catalog first and a function body resolves at call time.
    connection.execute(
        sql.SQL(
            """
            CREATE OR REPLACE FUNCTION {schema}.current_recovery_epoch()
            RETURNS bigint
            LANGUAGE sql
            STABLE
            AS $$
                SELECT COALESCE(
                    (SELECT value::bigint FROM {schema}.metadata
                      WHERE key = 'recovery_epoch'),
                    0
                )
            $$
            """
        ).format(schema=sql.Identifier(SCHEMA))
    )
    # Named here rather than taken from EPOCH_REVOKED_TABLES: a migration is a
    # historical fact, and reading a module constant makes its behaviour change
    # whenever that constant does. Migration 6 added a table to those lists and
    # this loop immediately tried to alter a table that would not exist for
    # another migration -- so a fresh database failed where an existing one had
    # succeeded. Each migration owns the objects it created.
    for table in _MIGRATION_5_TABLES:
        # A DEFAULT change is catalogue-only in PostgreSQL, so this rewrites no
        # table and takes no long lock however large they are.
        connection.execute(
            sql.SQL(
                "ALTER TABLE {schema}.{table}"
                " ALTER COLUMN recovery_epoch"
                " SET DEFAULT {schema}.current_recovery_epoch()"
            ).format(schema=sql.Identifier(SCHEMA), table=sql.Identifier(table))
        )


def _migration_6(connection: psycopg.Connection) -> None:
    """Rotating refresh families, with replay detection as a schema property.

    P6 approves rotating refresh for the P2 client ecosystems, and section 4
    fixes the bounds: at most 12 hours idle, 30 days absolute, rotated every
    use, and a replay invalidates the entire family *and its grant*.

    Two tables because there are two lifetimes. The family carries the
    authority -- the grant it belongs to, the scope it may refresh for, the
    absolute deadline and the revocation -- and each token is a leaf that lives
    until its idle deadline or until it is spent. Folding both into one table
    would mean recomputing family state from whichever row happened to be
    newest, and revoking a family would be a loop rather than one write.

    `replaced_by` records the rotation chain. It is not needed to authorise
    anything; it is what lets an operator see which token superseded which
    after a replay, which is the only forensic question worth asking here.

    No recovery_epoch on the tokens, deliberately. The family is the unit of
    authority, so revoking families at a restore kills every token in them --
    and the tokens are worth keeping as replay evidence rather than sweeping.
    """
    connection.execute(
        sql.SQL(
            """
            CREATE TABLE {schema}.oauth_refresh_families (
                family_id           text        PRIMARY KEY,
                -- The grant this family refreshes against. No foreign key: a
                -- refresh requires an *active* grant, which is an application
                -- check against revoked_at, and a constraint here would only
                -- restate the weaker half of it.
                grant_id            text        NOT NULL,
                client_id           text        NOT NULL
                    REFERENCES {schema}.oauth_clients(client_id),
                scope               text        NOT NULL,
                created_at          timestamptz NOT NULL DEFAULT now(),
                -- Absolute: 30 days from creation, and rotation never extends
                -- it. Only the idle deadline moves.
                absolute_expires_at timestamptz NOT NULL,
                revoked_at          timestamptz,
                revoked_reason      text,
                recovery_epoch      bigint      NOT NULL
                    DEFAULT {schema}.current_recovery_epoch(),
                CONSTRAINT refresh_family_reason_requires_revocation
                    CHECK (revoked_reason IS NULL OR revoked_at IS NOT NULL),
                CONSTRAINT refresh_family_absolute_after_creation
                    CHECK (absolute_expires_at > created_at)
            );

            CREATE TABLE {schema}.oauth_refresh_tokens (
                token_hash      text        PRIMARY KEY,
                family_id       text        NOT NULL
                    REFERENCES {schema}.oauth_refresh_families(family_id)
                    ON DELETE CASCADE,
                issued_at       timestamptz NOT NULL DEFAULT now(),
                -- Idle: 12 hours, and each rotation issues a fresh one.
                idle_expires_at timestamptz NOT NULL,
                -- Spent, not deleted. A replay has to arrive at a row that
                -- says when it was used, or a replayed token is
                -- indistinguishable from one that never existed -- and the two
                -- have opposite consequences.
                consumed_at     timestamptz,
                -- Which token superseded this one. There is no separate
                -- "consumed_by": for a refresh token the consumer *is* the
                -- rotation, so the successor's hash was the only value it
                -- could ever hold, and two columns holding one fact drift.
                replaced_by     text
                    REFERENCES {schema}.oauth_refresh_tokens(token_hash),
                CONSTRAINT refresh_token_replacement_requires_consumption
                    CHECK (replaced_by IS NULL OR consumed_at IS NOT NULL)
            );

            -- Every live token of one family, for revocation and for the
            -- bounded cleanup that expiry requires.
            CREATE INDEX oauth_refresh_tokens_family_idx
                ON {schema}.oauth_refresh_tokens (family_id);
            CREATE INDEX oauth_refresh_tokens_expiry_idx
                ON {schema}.oauth_refresh_tokens (idle_expires_at);
            -- A grant's live families, for revoking them with the grant.
            CREATE INDEX oauth_refresh_families_grant_idx
                ON {schema}.oauth_refresh_families (grant_id)
                WHERE revoked_at IS NULL;
            """
        ).format(schema=sql.Identifier(SCHEMA))
    )


MIGRATIONS = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
    5: _migration_5,
    6: _migration_6,
}


class IrreversibleMigration(RuntimeError):
    """Rolling back this far would destroy state that cannot be reconstructed."""


#: Which rollbacks lose data, and what. A rollback is refused unless the caller
#: names the loss, because the ladder's whole purpose is that a bad forward
#: migration can be undone -- and an operator who reaches for that under
#: pressure should not discover the cost afterwards.
#:
#: "Lossless" here means structural only: the columns and tables a rollback
#: removes carried no state the platform could not rebuild. Nothing about a
#: credential is ever reconstructible, so anything holding one is lossy.
DESTRUCTIVE_ROLLBACKS = {
    1: "every platform credential: the administrator password, CLI tokens,"
       " sessions and device authorizations",
    2: "every OAuth record: clients, authorization codes, tokens, pending"
       " authorizations and operator sessions",
    3: "the operation and request-digest binding on every issued token B,"
       " which is what confines one to a single request",
    4: "every grant -- the record of what an operator consented to, and the"
       " subject every issued credential resolves through",
    5: "the ability to invalidate restored credentials at all: the counter,"
       " the function and the column defaults. The sweep itself lives in"
       " application code, so after this rollback an advance refuses rather"
       " than half-working",
    6: "every rotating refresh family and its tokens, so each client has to"
       " complete a fresh authorization instead of refreshing",
}


def _rollback_1(connection: psycopg.Connection) -> None:
    connection.execute(
        sql.SQL(
            "DROP TABLE IF EXISTS {schema}.device_authorizations,"
            " {schema}.tokens, {schema}.sessions, {schema}.admin_credential,"
            " {schema}.metadata"
        ).format(schema=sql.Identifier(SCHEMA))
    )


def _rollback_2(connection: psycopg.Connection) -> None:
    """The five tables migration 2 creates. oauth_grants belongs to 4.

    Dropped in one statement: they carry client_id foreign keys into
    oauth_clients, so a single DROP avoids depending on whatever order
    PostgreSQL would otherwise pick.
    """
    connection.execute(
        sql.SQL(
            "DROP TABLE IF EXISTS {schema}.oauth_authorization_codes,"
            " {schema}.oauth_pending_authorizations, {schema}.oauth_tokens,"
            " {schema}.oauth_sessions, {schema}.oauth_clients"
        ).format(schema=sql.Identifier(SCHEMA))
    )


def _rollback_3(connection: psycopg.Connection) -> None:
    """Undo exactly what migration 3 added, and nothing migration 2 owns.

    `single_use` belongs to migration 2's CREATE TABLE, not here. An earlier
    draft of this dropped it, which left the schema unable to re-apply
    migration 3 at all -- its CHECK references that column. Caught by rolling
    the ladder down and back up rather than by reading it.

    The two CHECK constraints migration 3 added both reference columns dropped
    here, so PostgreSQL removes them with the columns; they are named anyway,
    because a reader should not have to know that rule to see the rollback is
    complete.
    """
    connection.execute(
        sql.SQL(
            "ALTER TABLE {schema}.oauth_tokens"
            "  DROP CONSTRAINT IF EXISTS token_single_use_is_bound,"
            "  DROP CONSTRAINT IF EXISTS token_operation_binding_is_complete,"
            "  DROP COLUMN IF EXISTS operation_id,"
            "  DROP COLUMN IF EXISTS request_digest,"
            "  DROP COLUMN IF EXISTS actor_client_id,"
            "  DROP COLUMN IF EXISTS broker_client_id"
        ).format(schema=sql.Identifier(SCHEMA))
    )


def _rollback_4(connection: psycopg.Connection) -> None:
    """Migration 4 creates oauth_grants, so rolling it back drops the grants.

    An earlier draft assumed migration 2 owned that table and dropped only the
    two indexes -- which left oauth_grants in place and made re-applying
    migration 4 fail on a duplicate table. Caught by rolling the ladder down
    and back up rather than by reading it.

    oauth_grants_live_idx is on that table and goes with it;
    oauth_tokens_subject_idx is on oauth_tokens, which migration 2 owns, so it
    has to be dropped explicitly.
    """
    connection.execute(
        sql.SQL(
            "DROP INDEX IF EXISTS {schema}.oauth_tokens_subject_idx"
        ).format(schema=sql.Identifier(SCHEMA))
    )
    connection.execute(
        sql.SQL("DROP TABLE IF EXISTS {schema}.oauth_grants").format(
            schema=sql.Identifier(SCHEMA)
        )
    )


def _rollback_5(connection: psycopg.Connection) -> None:
    """Put the epoch back to reserved storage.

    Loses the counter, the function and every column default, which is why this
    is declared destructive. No credential state goes with it: an advance
    records itself as a revocation and those survive. What goes is the ability
    to perform another one -- advance_recovery_epoch checks for the function and
    refuses without it, which is the refusal the restore procedure documents.

    An earlier version of this note claimed a later advance "would restart from
    zero and would not invalidate anything already stamped above it". That
    described the read-time-predicate design this migration deliberately
    rejected; the sweep never reads the stamp.
    """
    for table in _MIGRATION_5_TABLES:
        connection.execute(
            sql.SQL(
                "ALTER TABLE {schema}.{table}"
                " ALTER COLUMN recovery_epoch SET DEFAULT 0"
            ).format(schema=sql.Identifier(SCHEMA), table=sql.Identifier(table))
        )
    # After the defaults, and CASCADE. Migration 6's oauth_refresh_families
    # also defaults to this function, so a plain DROP succeeds only while the
    # ladder happens to descend newest-first and _rollback_6 has already
    # removed that table. Depending on the iteration order of a different
    # function for correctness here is exactly the coupling that made
    # migration 5 read a live constant in the first place.
    connection.execute(
        sql.SQL(
            "DROP FUNCTION IF EXISTS {schema}.current_recovery_epoch() CASCADE"
        ).format(schema=sql.Identifier(SCHEMA))
    )
    connection.execute(
        sql.SQL(
            "DELETE FROM {schema}.metadata WHERE key = 'recovery_epoch'"
        ).format(schema=sql.Identifier(SCHEMA))
    )


def _rollback_6(connection: psycopg.Connection) -> None:
    """Tokens before families: the tokens reference them.

    ON DELETE CASCADE would cope, but naming the order means the statement
    says what it depends on rather than relying on a clause three tables away.
    """
    connection.execute(
        sql.SQL(
            "DROP TABLE IF EXISTS {schema}.oauth_refresh_tokens,"
            " {schema}.oauth_refresh_families"
        ).format(schema=sql.Identifier(SCHEMA))
    )


ROLLBACKS = {
    1: _rollback_1,
    2: _rollback_2,
    3: _rollback_3,
    4: _rollback_4,
    5: _rollback_5,
    6: _rollback_6,
}


def applied_versions(connection: psycopg.Connection) -> list[int]:
    """Which migrations this database has, newest last.

    Reads the ledger rather than inspecting the schema: a rollback has to undo
    what was recorded as applied, not what somebody's DDL happens to look like.
    """
    rows = connection.execute(
        sql.SQL(
            "SELECT version FROM {schema}.schema_migrations ORDER BY version"
        ).format(schema=sql.Identifier(SCHEMA))
    ).fetchall()
    return [int(row["version"]) for row in rows]


def rollback(
    connection: psycopg.Connection,
    to_version: int,
    *,
    accept_data_loss: bool = False,
) -> list[int]:
    """Step the ladder down to ``to_version``. Returns the versions undone.

    The forward half has existed since M4 and this half has not, which made
    every schema change after it a one-way door: a bad migration could only be
    fixed by another migration, under whatever pressure caused the first one.

    Same advisory lock and same single transaction as ``migrate``, so a
    concurrent start blocks rather than racing a ``DROP TABLE``, and a failure
    part-way leaves the ledger and the schema agreeing with each other.

    Refuses a destructive step unless ``accept_data_loss`` is set, and names
    what would be lost. PostgreSQL DDL is transactional here, but no
    transaction brings back a dropped credential.
    """
    if to_version < 0:
        raise ValueError("to_version must be zero or a migration version.")
    undone: list[int] = []
    connection.execute("BEGIN")
    try:
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(MIGRATION_TIMEOUT_MS),),
        )
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (MIGRATION_LOCK,)
        )
        # Newest first: a rollback undoes in the reverse of the order applied,
        # or migration 2's DROP would run while migration 3's columns still
        # depend on its table.
        for version in sorted(applied_versions(connection), reverse=True):
            if version <= to_version:
                break
            if version not in ROLLBACKS:
                raise IrreversibleMigration(
                    f"Migration {version} has no rollback, so the ladder cannot"
                    f" step below {version}."
                )
            loss = DESTRUCTIVE_ROLLBACKS.get(version)
            if loss and not accept_data_loss:
                raise IrreversibleMigration(
                    f"Rolling back migration {version} destroys {loss}."
                    " Re-run with accept_data_loss=True if that is intended."
                )
            ROLLBACKS[version](connection)
            connection.execute(
                sql.SQL(
                    "DELETE FROM {schema}.schema_migrations WHERE version = %s"
                ).format(schema=sql.Identifier(SCHEMA)),
                (version,),
            )
            undone.append(version)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return undone


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
