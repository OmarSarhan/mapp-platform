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

            CREATE INDEX oauth_grants_live_idx
                ON {schema}.oauth_grants (client_id) WHERE revoked_at IS NULL;

            -- No separate grant_id column: `subject` already holds the grant
            -- id. authenticate_user returns it off the authorization code and
            -- the token inherits it, so a second column would be the same
            -- value written twice and free to drift. This index is what
            -- introspection's grant lookup uses.
            --
            -- No foreign key either, deliberately. Rows written before grants
            -- existed carry an operator name rather than a grant id, and a
            -- constraint would fail the migration on them. Introspection
            -- treats a subject that resolves to no grant as inactive, so the
            -- absence fails closed without needing the database to enforce it.
            CREATE INDEX oauth_tokens_subject_idx ON {schema}.oauth_tokens (subject);
            """
        ).format(schema=sql.Identifier(SCHEMA))
    )


MIGRATIONS = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
