from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Sequence
from typing import Any

from psycopg import Connection, OperationalError, connect, sql
from psycopg.types.json import Jsonb

from .config import AppConfig, LayerConfig
from .core import LayerInspection, PreparedFeature


LOGGER = logging.getLogger(__name__)

TYPE_SQL = {
    "text": sql.SQL("text"),
    "integer": sql.SQL("integer"),
    "bigint": sql.SQL("bigint"),
    "double precision": sql.SQL("double precision"),
    "boolean": sql.SQL("boolean"),
    "date": sql.SQL("date"),
    "timestamptz": sql.SQL("timestamp with time zone"),
    "jsonb": sql.SQL("jsonb"),
}


class DatabaseError(RuntimeError):
    pass


def connect_with_retry(
    database_url: str,
    *,
    attempts: int = 12,
    initial_delay_seconds: float = 1.0,
) -> Connection[Any]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return connect(database_url, connect_timeout=10)
        except OperationalError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            delay = min(10.0, initial_delay_seconds * (1.5**attempt))
            LOGGER.warning(
                "database is unavailable (attempt %s/%s); retrying in %.1fs",
                attempt + 1,
                attempts,
                delay,
            )
            time.sleep(delay)
    raise DatabaseError(
        f"could not connect to PostgreSQL after {attempts} attempts: {last_error}"
    ) from last_error


class PostgresStore:
    def __init__(self, connection: Connection[Any], config: AppConfig) -> None:
        self.connection = connection
        self.config = config
        self.schema = config.target_schema

    def close(self) -> None:
        self.connection.close()

    def _commit_or_rollback(self, operation: Any) -> Any:
        try:
            result = operation()
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    def _layer_lock_key(self, layer: LayerConfig) -> str:
        return f"mapp-explore-etl:{self.schema}.{layer.target_table}"

    def acquire_layer_lock(self, layer: LayerConfig) -> bool:
        """Take a session lock so overlapping runs cannot reconcile one table."""

        def operation() -> bool:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (self._layer_lock_key(layer),),
                )
                row = cursor.fetchone()
                return bool(row and row[0] is True)

        return bool(self._commit_or_rollback(operation))

    def release_layer_lock(self, layer: LayerConfig) -> None:
        def operation() -> bool:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (self._layer_lock_key(layer),),
                )
                row = cursor.fetchone()
                return bool(row and row[0] is True)

        released = bool(self._commit_or_rollback(operation))
        if not released:
            LOGGER.warning("layer advisory lock was not held for %s", layer.key)

    def initialize(self) -> None:
        def operation() -> None:
            with self.connection.cursor() as cursor:
                try:
                    cursor.execute("SELECT PostGIS_Version()")
                    cursor.fetchone()
                except Exception as exc:
                    raise DatabaseError(
                        "PostGIS is not installed or is not visible to the ETL role; "
                        "install the extension as a database administrator"
                    ) from exc
                cursor.execute(
                    """
                    SELECT has_schema_privilege(current_user, %s, 'USAGE')
                       AND has_schema_privilege(current_user, %s, 'CREATE')
                    """,
                    (self.schema, self.schema),
                )
                schema_is_writable = cursor.fetchone()
                if not schema_is_writable or schema_is_writable[0] is not True:
                    raise DatabaseError(
                        f"schema {self.schema!r} does not exist or is not writable by "
                        "the ETL role; create it as an administrator and grant "
                        "USAGE, CREATE"
                    )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}._etl_runs (
                            run_id uuid PRIMARY KEY,
                            layer_key text NOT NULL,
                            source_url text NOT NULL,
                            status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
                            started_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            finished_at timestamp with time zone,
                            expected_count bigint,
                            ending_count bigint,
                            rows_seen bigint NOT NULL DEFAULT 0,
                            rows_deleted bigint NOT NULL DEFAULT 0,
                            error text
                        )
                        """
                    ).format(sql.Identifier(self.schema))
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE INDEX IF NOT EXISTS _etl_runs_layer_started_idx
                        ON {}._etl_runs (layer_key, started_at DESC)
                        """
                    ).format(sql.Identifier(self.schema))
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}._etl_layers (
                            layer_key text PRIMARY KEY,
                            source_url text NOT NULL,
                            source_name text,
                            source_srid integer,
                            target_table text NOT NULL,
                            geometry_type text NOT NULL,
                            source_metadata jsonb,
                            last_success_at timestamp with time zone,
                            last_successful_run_id uuid
                        )
                        """
                    ).format(sql.Identifier(self.schema))
                )
                # Deliberately not underscore-prefixed: semantic sync
                # excludes "_"-prefixed relations from discovery, and the
                # federation verifier must be able to read this record.
                # Exactly one row — the current release — enforced
                # by the boolean singleton primary key.
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.dataset_publication (
                            singleton boolean PRIMARY KEY DEFAULT true
                                CHECK (singleton),
                            dataset_id text NOT NULL,
                            release_id text NOT NULL UNIQUE,
                            schema_version integer NOT NULL,
                            source_hash text NOT NULL,
                            published_at timestamp with time zone NOT NULL,
                            row_counts jsonb NOT NULL,
                            geometry_contract_version integer NOT NULL
                        )
                        """
                    ).format(sql.Identifier(self.schema))
                )
                for layer in self.config.layers:
                    self._ensure_layer_table(cursor, layer)

        self._commit_or_rollback(operation)

    def _ensure_layer_table(self, cursor: Any, layer: LayerConfig) -> None:
        column_definitions = [
            sql.SQL("{} {}").format(
                sql.Identifier(column.target), TYPE_SQL[column.postgres_type]
            )
            for column in layer.columns
        ]
        geometry_type = sql.SQL(layer.target_geometry_type)
        cursor.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.{} (
                    object_id bigint PRIMARY KEY,
                    {},
                    source_attributes jsonb NOT NULL,
                    geom geometry({}, 4326),
                    geom_3857 geometry(Geometry, 3857)
                        GENERATED ALWAYS AS (ST_Transform(geom, 3857)) STORED,
                    source_hash text NOT NULL,
                    first_seen_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_changed_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_run_id uuid NOT NULL
                )
                """
            ).format(
                sql.Identifier(self.schema),
                sql.Identifier(layer.target_table),
                sql.SQL(", ").join(column_definitions),
                geometry_type,
            )
        )
        for column in layer.columns:
            cursor.execute(
                sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {} {}").format(
                    sql.Identifier(self.schema),
                    sql.Identifier(layer.target_table),
                    sql.Identifier(column.target),
                    TYPE_SQL[column.postgres_type],
                )
            )
        cursor.execute(
            sql.SQL(
                """
                ALTER TABLE {}.{}
                ADD COLUMN IF NOT EXISTS geom_3857 geometry(Geometry, 3857)
                    GENERATED ALWAYS AS (ST_Transform(geom, 3857)) STORED
                """
            ).format(
                sql.Identifier(self.schema),
                sql.Identifier(layer.target_table),
            )
        )
        cursor.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} USING gist (geom)").format(
                sql.Identifier(f"{layer.target_table}_geom_gix"),
                sql.Identifier(self.schema),
                sql.Identifier(layer.target_table),
            )
        )
        cursor.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (last_seen_run_id)").format(
                sql.Identifier(f"{layer.target_table}_seen_run_idx"),
                sql.Identifier(self.schema),
                sql.Identifier(layer.target_table),
            )
        )
        cursor.execute(
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {}.{} USING gist (geom_3857)"
            ).format(
                sql.Identifier(f"{layer.target_table}_geom_3857_gix"),
                sql.Identifier(self.schema),
                sql.Identifier(layer.target_table),
            )
        )

    def start_run(self, layer: LayerConfig, run_id: uuid.UUID) -> None:
        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}._etl_runs
                            (run_id, layer_key, source_url, status)
                        VALUES (%s, %s, %s, 'running')
                        """
                    ).format(sql.Identifier(self.schema)),
                    (run_id, layer.key, layer.source_url),
                )

        self._commit_or_rollback(operation)

    def register_layer(
        self,
        layer: LayerConfig,
        inspection: LayerInspection,
        metadata: dict[str, Any],
        run_id: uuid.UUID,
        expected_count: int,
    ) -> None:
        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}._etl_layers
                            (layer_key, source_url, source_name, source_srid,
                             target_table, geometry_type, source_metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (layer_key) DO UPDATE SET
                            source_url = EXCLUDED.source_url,
                            source_name = EXCLUDED.source_name,
                            source_srid = EXCLUDED.source_srid,
                            target_table = EXCLUDED.target_table,
                            geometry_type = EXCLUDED.geometry_type,
                            source_metadata = EXCLUDED.source_metadata
                        """
                    ).format(sql.Identifier(self.schema)),
                    (
                        layer.key,
                        layer.source_url,
                        inspection.name,
                        inspection.source_srid,
                        layer.target_table,
                        layer.target_geometry_type,
                        Jsonb(metadata),
                    ),
                )
                cursor.execute(
                    sql.SQL(
                        "UPDATE {}._etl_runs SET expected_count = %s WHERE run_id = %s"
                    ).format(sql.Identifier(self.schema)),
                    (expected_count, run_id),
                )

        self._commit_or_rollback(operation)

    def _geometry_expression(self, layer: LayerConfig) -> sql.SQL:
        base = sql.SQL("ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))")
        if layer.target_geometry_type in {"MultiLineString", "MultiPolygon"}:
            return sql.SQL("ST_Multi({})").format(base)
        return base

    def upsert_page(
        self,
        layer: LayerConfig,
        features: Sequence[PreparedFeature],
        run_id: uuid.UUID,
    ) -> None:
        if not features:
            return
        data_columns = [sql.Identifier(column.target) for column in layer.columns]
        insert_columns = [
            sql.Identifier("object_id"),
            *data_columns,
            sql.Identifier("source_attributes"),
            sql.Identifier("geom"),
            sql.Identifier("source_hash"),
            sql.Identifier("last_seen_run_id"),
        ]
        placeholders = [sql.Placeholder()] * (1 + len(layer.columns))
        placeholders.extend(
            [
                sql.Placeholder(),
                self._geometry_expression(layer),
                sql.Placeholder(),
                sql.Placeholder(),
            ]
        )
        assignments = [
            sql.SQL("{} = EXCLUDED.{}").format(column, column)
            for column in data_columns
        ]
        assignments.extend(
            [
                sql.SQL("source_attributes = EXCLUDED.source_attributes"),
                sql.SQL("geom = EXCLUDED.geom"),
                sql.SQL(
                    """
                    last_changed_at = CASE
                        WHEN target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
                        THEN CURRENT_TIMESTAMP
                        ELSE target.last_changed_at
                    END
                    """
                ),
                sql.SQL("source_hash = EXCLUDED.source_hash"),
                sql.SQL("last_seen_at = CURRENT_TIMESTAMP"),
                sql.SQL("last_seen_run_id = EXCLUDED.last_seen_run_id"),
            ]
        )
        statement = sql.SQL(
            """
            INSERT INTO {}.{} AS target ({})
            VALUES ({})
            ON CONFLICT (object_id) DO UPDATE SET {}
            """
        ).format(
            sql.Identifier(self.schema),
            sql.Identifier(layer.target_table),
            sql.SQL(", ").join(insert_columns),
            sql.SQL(", ").join(placeholders),
            sql.SQL(", ").join(assignments),
        )
        rows = [
            (
                feature.object_id,
                *feature.values,
                Jsonb(feature.source_attributes),
                (
                    json.dumps(
                        feature.geometry,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    if feature.geometry is not None
                    else None
                ),
                feature.source_hash,
                run_id,
            )
            for feature in features
        ]

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.executemany(statement, rows)

        self._commit_or_rollback(operation)

    def reconcile(self, layer: LayerConfig, run_id: uuid.UUID) -> int:
        def operation() -> int:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DELETE FROM {}.{} WHERE last_seen_run_id <> %s").format(
                        sql.Identifier(self.schema),
                        sql.Identifier(layer.target_table),
                    ),
                    (run_id,),
                )
                return cursor.rowcount

        return int(self._commit_or_rollback(operation))

    def finish_run(
        self,
        layer: LayerConfig,
        run_id: uuid.UUID,
        *,
        rows_seen: int,
        rows_deleted: int,
        ending_count: int,
    ) -> None:
        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ANALYZE {}.{}").format(
                        sql.Identifier(self.schema),
                        sql.Identifier(layer.target_table),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        """
                        UPDATE {}._etl_runs
                        SET status = 'succeeded', finished_at = CURRENT_TIMESTAMP,
                            ending_count = %s, rows_seen = %s, rows_deleted = %s
                        WHERE run_id = %s AND status = 'running'
                        """
                    ).format(sql.Identifier(self.schema)),
                    (ending_count, rows_seen, rows_deleted, run_id),
                )
                cursor.execute(
                    sql.SQL(
                        """
                        UPDATE {}._etl_layers
                        SET last_success_at = CURRENT_TIMESTAMP,
                            last_successful_run_id = %s
                        WHERE layer_key = %s
                        """
                    ).format(sql.Identifier(self.schema)),
                    (run_id, layer.key),
                )

        self._commit_or_rollback(operation)

    def fail_run(self, run_id: uuid.UUID, error: str, rows_seen: int) -> None:
        message = error[:10_000]

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        UPDATE {}._etl_runs
                        SET status = 'failed', finished_at = CURRENT_TIMESTAMP,
                            rows_seen = %s, error = %s
                        WHERE run_id = %s AND status = 'running'
                        """
                    ).format(sql.Identifier(self.schema)),
                    (rows_seen, message, run_id),
                )

        try:
            self.connection.rollback()
            self._commit_or_rollback(operation)
        except Exception:
            LOGGER.exception("could not record failed ETL run %s", run_id)

    def publish_release(
        self,
        *,
        dataset_id: str,
        release_id: str,
        schema_version: int,
        source_hash: str,
        geometry_contract_version: int,
    ) -> None:
        """Atomically publish the dataset_publication record for this schema.

        Call this once, as the last step of a fully successful ETL run —
        after every layer's finish_run() has already committed — never per
        layer. row_counts is computed from every configured layer's target
        table inside this same transaction, so the published counts can
        never disagree with the row_counts this call records; if anything
        raises, _commit_or_rollback rolls the whole write back and the
        previous release's record is left intact, per the federation
        architecture waypoint's atomic ETL boundary.
        """

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "SELECT schema_version, geometry_contract_version "
                        "FROM {}.dataset_publication WHERE singleton"
                    ).format(sql.Identifier(self.schema))
                )
                previous = cursor.fetchone()
                if previous is not None:
                    previous_schema_version, previous_geometry_version = previous
                    if schema_version < previous_schema_version:
                        raise DatabaseError(
                            "schema_version must not regress: "
                            f"{schema_version} < {previous_schema_version}"
                        )
                    if geometry_contract_version < previous_geometry_version:
                        raise DatabaseError(
                            "geometry_contract_version must not regress: "
                            f"{geometry_contract_version} < "
                            f"{previous_geometry_version}"
                        )

                row_counts: dict[str, int] = {}
                for layer in self.config.layers:
                    cursor.execute(
                        sql.SQL("SELECT count(*) FROM {}.{}").format(
                            sql.Identifier(self.schema),
                            sql.Identifier(layer.target_table),
                        )
                    )
                    row_counts[layer.target_table] = cursor.fetchone()[0]

                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.dataset_publication
                            (singleton, dataset_id, release_id, schema_version,
                             source_hash, published_at, row_counts,
                             geometry_contract_version)
                        VALUES (true, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s)
                        ON CONFLICT (singleton) DO UPDATE SET
                            dataset_id = EXCLUDED.dataset_id,
                            release_id = EXCLUDED.release_id,
                            schema_version = EXCLUDED.schema_version,
                            source_hash = EXCLUDED.source_hash,
                            published_at = EXCLUDED.published_at,
                            row_counts = EXCLUDED.row_counts,
                            geometry_contract_version =
                                EXCLUDED.geometry_contract_version
                        """
                    ).format(sql.Identifier(self.schema)),
                    (
                        dataset_id,
                        release_id,
                        schema_version,
                        source_hash,
                        Jsonb(row_counts),
                        geometry_contract_version,
                    ),
                )

        self._commit_or_rollback(operation)
