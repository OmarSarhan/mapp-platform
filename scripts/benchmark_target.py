"""Read-only checks before a benchmark may migrate or seed its control schema."""

import control_schema as cs
from psycopg import sql


class RefusedDestructiveRun(RuntimeError):
    """The control schema does not match the requested disposable fixture."""


def guard_scratch_database(dsn: str, *, require_fresh: bool = False, force: bool = False) -> None:
    with cs.connect(dsn) as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL row_security = off")
        schema = conn.execute(
            "SELECT EXISTS (SELECT FROM pg_depend WHERE refclassid = 'pg_namespace'::regclass"
            " AND refobjid = n.oid) AS has_objects FROM pg_namespace n WHERE nspname = 'control'"
        ).fetchone()
        if schema and not schema["has_objects"]:
            return
        if not schema or require_fresh:
            raise RefusedDestructiveRun("Use a fresh disposable database with an empty control schema.")

        # A completed benchmark leaves a migrated schema. Only its ledger and
        # migration-seeded zero epoch may remain in an ordinary repeat run.
        relations = conn.execute(
            "SELECT relname, relkind FROM pg_class WHERE relnamespace = 'control'::regnamespace"
            " AND relkind NOT IN ('i', 'S', 'I')"
        )
        actual = {(r["relname"], r["relkind"]) for r in relations}
        expected = {(name, "r") for name in (*cs.TABLES_IN_DELETE_ORDER, "schema_migrations")}
        if (
            actual != expected
            or cs._restore_schema_signature(conn) != cs.RESTORE_SCHEMA_SIGNATURES[max(cs.MIGRATIONS)]
            or cs.applied_versions(conn) != sorted(cs.MIGRATIONS)
        ):
            raise RefusedDestructiveRun("Refusing a partial or unfamiliar control schema.")
        if force:
            return
        for table in cs.TABLES_IN_DELETE_ORDER:
            query = sql.SQL("SELECT 1 FROM control.{}").format(sql.Identifier(table))
            if table == "metadata":
                query += sql.SQL(" WHERE (key, value) <> ('recovery_epoch', '0')")
            if conn.execute(query + sql.SQL(" LIMIT 1")).fetchone():
                raise RefusedDestructiveRun("Refusing a populated control schema. Use an empty scratch database.")
