"""The scratch PostgreSQL database both semantic-service suites run against.

The store's SQL is the thing under test, and a mocked cursor cannot fail the
way PostgreSQL fails when it parses a statement, so the tests talk to a real
server.  Set ``SEMANTIC_TEST_DATABASE_URL`` to a scratch database; without it
the suites skip rather than pass on nothing.

The schema named ``semantic`` in that database is dropped and recreated before
every test, so it must not be a database anyone cares about.  Point it at a
database created for the purpose, never at the packaged catalogue.
"""

from __future__ import annotations

import os
import unittest

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from semantic_store import SCHEMA, SemanticStore, StoreConnection


DATABASE_URL = os.getenv("SEMANTIC_TEST_DATABASE_URL", "")

# The deployed reader is a role with no write privilege on anything.  One
# scratch role stands in for both here, so the read-only half is expressed as
# a connection that refuses to write: a read path that quietly writes then
# fails in the tests as loudly as it would in production.
READER_DATABASE_URL = (
    make_conninfo(DATABASE_URL, options="-c default_transaction_read_only=on")
    if DATABASE_URL
    else ""
)

requires_database = unittest.skipUnless(
    DATABASE_URL,
    "set SEMANTIC_TEST_DATABASE_URL to a scratch PostgreSQL database to run"
    f" the semantic service tests; its {SCHEMA} schema is dropped and"
    " recreated before every test",
)


def connect() -> StoreConnection:
    """A writable connection carrying the store's own search path.

    Unqualified table names therefore resolve the way the store's SQL resolves
    them.  New objects do not: `pg_catalog` comes first, so DDL written
    through this has to name the schema.
    """
    connection = psycopg.connect(
        DATABASE_URL, autocommit=True, row_factory=dict_row
    )
    connection.execute(
        sql.SQL("SET SESSION search_path = pg_catalog, {schema}").format(
            schema=sql.Identifier(SCHEMA)
        )
    )
    return connection


def reset_schema() -> None:
    """Empty the store's schema without migrating it."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE").format(
                schema=sql.Identifier(SCHEMA)
            )
        )
        connection.execute(
            sql.SQL("CREATE SCHEMA {schema}").format(schema=sql.Identifier(SCHEMA))
        )


def open_store() -> SemanticStore:
    """A store on whatever is already in the schema, as a restart would find it."""
    return SemanticStore(DATABASE_URL, READER_DATABASE_URL)


def fresh_store() -> SemanticStore:
    """An empty store, migrated from nothing, as every test starts."""
    reset_schema()
    return open_store()
