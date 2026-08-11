from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg import Connection, errors, sql
from psycopg.types.json import Jsonb

from .config import IDENTIFIER_RE, ColumnConfig
from .core import PreparedFeature
from .database import TYPE_SQL

if TYPE_CHECKING:
    from .census_config import CensusConfig


LOGGER = logging.getLogger(__name__)

DATASET_METADATA_TABLE = "census_datasets"
VARIABLE_METADATA_TABLE = "census_variables"
DATASET_PUBLICATION_TABLE = "dataset_publication"
RUN_TABLE = "_census_etl_runs"
ABANDONED_RUN_ERROR = (
    "abandoned: previous Census ETL session ended before completion"
)
TABLE_COMMENT = (
    "ONS Census 2021 summary statistics for England at 2021 Output Area "
    "level. Published values may differ between tables because of statistical "
    "disclosure control. See leeds.census_datasets and "
    "leeds.census_variables for official source metadata."
)
OA_CODE_COMMENT = (
    "Official 2021 Output Area code for England (OA21CD); stable feature "
    "identifier."
)
GEOMETRY_COMMENT = (
    "ONS Output Areas (December 2021) EW BGC V2 source geometry, normalized "
    "to a valid MultiPolygon in EPSG:4326."
)
GEOMETRY_3857_COMMENT = (
    "Stored generated EPSG:3857 transform of geom for map rendering."
)
VARIABLE_COMMENT_MAX_CHARS = 2_000


class CensusDatabaseError(RuntimeError):
    pass


class CensusDuplicateCodeError(CensusDatabaseError):
    pass


class CensusCodeSetError(CensusDatabaseError):
    pass


@dataclass(frozen=True)
class CensusVariableMetadata:
    column_name: str
    topic_id: str
    topic_title: str
    ordinal: int
    label: str
    source_url: str
    source_member: str
    source_sha256: str
    source_metadata: dict[str, Any]


@dataclass(frozen=True)
class CensusDatasetMetadata:
    dataset_key: str
    oa_count: int
    variable_count: int
    geometry_repairs: int
    geometry_source_url: str
    geometry_source_sha256: str
    source_metadata: dict[str, Any]


def _require_identifier(value: str, context: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise CensusDatabaseError(
            f"{context} must be a lowercase PostgreSQL identifier"
        )
    return value


def _variable_comment(variable: CensusVariableMetadata) -> str:
    if (
        not isinstance(variable.label, str)
        or not variable.label.strip()
        or "\x00" in variable.label
    ):
        raise CensusDatabaseError(
            f"{variable.column_name}: Census variable label is invalid"
        )
    comment = (
        f"Census 2021 {variable.topic_id} measure {variable.ordinal}: "
        f"{variable.label}"
    )
    if len(comment) > VARIABLE_COMMENT_MAX_CHARS:
        raise CensusDatabaseError(
            f"{variable.column_name}: Census variable comment exceeds "
            f"{VARIABLE_COMMENT_MAX_CHARS} characters"
        )
    return comment


class CensusPostgresStore:
    """Database operations for an all-or-nothing Census publication.

    Network reads are intentionally performed outside a long transaction. The
    run's wide and per-topic staging tables are session-local temporary tables
    that survive intermediate commits. Only ``publish`` mutates the stable
    target and public metadata, in one atomic publication transaction.
    """

    def __init__(
        self,
        connection: Connection[Any],
        config: CensusConfig,
    ) -> None:
        self.connection = connection
        self.config = config
        self.schema = _require_identifier(config.target_schema, "target_schema")
        self.target_table = _require_identifier(config.target_table, "target_table")
        self.geometry_columns = tuple(config.geometry_layer.columns)
        self.statistic_columns = tuple(
            column
            for topic in config.topics
            for column in topic.target_columns
        )
        self._validate_columns()

    def _validate_columns(self) -> None:
        geometry_names = [column.target for column in self.geometry_columns]
        statistic_names = list(self.statistic_columns)
        for name in (*geometry_names, *statistic_names):
            _require_identifier(name, "census column")
        if geometry_names.count("oa21cd") != 1:
            raise CensusDatabaseError(
                "geometry columns must contain oa21cd exactly once"
            )
        oa_column = self.geometry_columns[geometry_names.index("oa21cd")]
        if oa_column.postgres_type != "text":
            raise CensusDatabaseError("geometry column oa21cd must use text")
        all_names = [*geometry_names, *statistic_names]
        if len(all_names) != len(set(all_names)):
            raise CensusDatabaseError("census target columns must be unique")
        reserved = {"geom", "geom_3857"}
        conflicts = reserved.intersection(all_names)
        if conflicts:
            raise CensusDatabaseError(
                "census columns conflict with managed columns: "
                f"{', '.join(sorted(conflicts))}"
            )

    def _commit_or_rollback(self, operation: Any) -> Any:
        try:
            result = operation()
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    @property
    def _lock_key(self) -> str:
        return f"mapp-explore-etl-census:{self.schema}.{self.target_table}"

    def acquire_lock(self) -> bool:
        def operation() -> bool:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (self._lock_key,),
                )
                row = cursor.fetchone()
                return bool(row and row[0] is True)

        return bool(self._commit_or_rollback(operation))

    def release_lock(self) -> None:
        def operation() -> bool:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (self._lock_key,),
                )
                row = cursor.fetchone()
                return bool(row and row[0] is True)

        if not self._commit_or_rollback(operation):
            LOGGER.warning("census advisory lock was not held")

    def _wide_column_definitions(self) -> list[sql.Composed]:
        definitions = [
            sql.SQL("{} {}").format(
                sql.Identifier(column.target),
                TYPE_SQL[column.postgres_type],
            )
            for column in self.geometry_columns
            if column.target != "oa21cd"
        ]
        definitions.extend(
            sql.SQL("{} double precision").format(sql.Identifier(column))
            for column in self.statistic_columns
        )
        return definitions

    def initialize(self) -> None:
        """Create stable relations without replacing an existing dataset."""

        wide_definitions = self._wide_column_definitions()

        def operation() -> None:
            with self.connection.cursor() as cursor:
                try:
                    cursor.execute("SELECT PostGIS_Version()")
                    cursor.fetchone()
                except Exception as exc:
                    raise CensusDatabaseError(
                        "PostGIS is not installed or is not visible to the "
                        "Census ETL role"
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
                    raise CensusDatabaseError(
                        f"schema {self.schema!r} does not exist or is not "
                        "writable by the Census ETL role; grant USAGE, CREATE"
                    )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.{} (
                            oa21cd text PRIMARY KEY,
                            {},
                            geom geometry(MultiPolygon, 4326) NOT NULL,
                            geom_3857 geometry(MultiPolygon, 3857)
                                GENERATED ALWAYS AS
                                    (ST_Transform(geom, 3857)) STORED
                        )
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(self.target_table),
                        sql.SQL(", ").join(wide_definitions),
                    )
                )
                for column in self.geometry_columns:
                    if column.target == "oa21cd":
                        continue
                    cursor.execute(
                        sql.SQL(
                            "ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {} {}"
                        ).format(
                            sql.Identifier(self.schema),
                            sql.Identifier(self.target_table),
                            sql.Identifier(column.target),
                            TYPE_SQL[column.postgres_type],
                        )
                    )
                for statistic_column in self.statistic_columns:
                    cursor.execute(
                        sql.SQL(
                            "ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {} "
                            "double precision"
                        ).format(
                            sql.Identifier(self.schema),
                            sql.Identifier(self.target_table),
                            sql.Identifier(statistic_column),
                        )
                    )
                cursor.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {} ON {}.{} USING gist (geom)"
                    ).format(
                        sql.Identifier(f"{self.target_table}_geom_gix"),
                        sql.Identifier(self.schema),
                        sql.Identifier(self.target_table),
                    )
                )
                expected_types = {
                    "oa21cd": "text",
                    **{
                        column.target: (
                            "timestamp with time zone"
                            if column.postgres_type == "timestamptz"
                            else column.postgres_type
                        )
                        for column in self.geometry_columns
                        if column.target != "oa21cd"
                    },
                    **{
                        column: "double precision"
                        for column in self.statistic_columns
                    },
                    "geom": "geometry(MultiPolygon,4326)",
                    "geom_3857": "geometry(MultiPolygon,3857)",
                }
                cursor.execute(
                    """
                    SELECT attribute.attname,
                           pg_catalog.format_type(
                               attribute.atttypid,
                               attribute.atttypmod
                           ),
                           attribute.attgenerated,
                           attribute.attnotnull,
                           EXISTS (
                               SELECT 1
                               FROM pg_catalog.pg_constraint AS primary_key
                               WHERE primary_key.conrelid = relation.oid
                                 AND primary_key.contype = 'p'
                                 AND primary_key.conkey =
                                     ARRAY[attribute.attnum]::smallint[]
                           ) AS is_sole_primary_key
                    FROM pg_catalog.pg_attribute AS attribute
                    JOIN pg_catalog.pg_class AS relation
                      ON relation.oid = attribute.attrelid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = %s
                      AND relation.relname = %s
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                    """,
                    (self.schema, self.target_table),
                )
                actual_rows = cursor.fetchall()
                actual_types = {
                    str(name): str(postgres_type)
                    for (
                        name,
                        postgres_type,
                        _generated,
                        _not_null,
                        _primary_key,
                    ) in actual_rows
                }
                generated = {
                    str(name): str(generation)
                    for (
                        name,
                        _postgres_type,
                        generation,
                        _not_null,
                        _primary_key,
                    ) in actual_rows
                    if generation
                }
                not_null = {
                    str(name)
                    for (
                        name,
                        _postgres_type,
                        _generation,
                        required,
                        _primary_key,
                    ) in actual_rows
                    if required
                }
                primary_key = {
                    str(name)
                    for (
                        name,
                        _postgres_type,
                        _generation,
                        _not_null,
                        is_primary_key,
                    ) in actual_rows
                    if is_primary_key
                }
                if (
                    actual_types != expected_types
                    or generated != {"geom_3857": "s"}
                    or "geom" not in not_null
                    or primary_key != {"oa21cd"}
                ):
                    missing = sorted(expected_types.keys() - actual_types.keys())
                    extra = sorted(actual_types.keys() - expected_types.keys())
                    wrong_types = sorted(
                        name
                        for name in expected_types.keys() & actual_types.keys()
                        if expected_types[name] != actual_types[name]
                    )
                    raise CensusDatabaseError(
                        "stable Census table schema differs from the reviewed "
                        f"contract (missing={missing}, extra={extra}, "
                        f"wrong_types={wrong_types}, "
                        f"generated={generated}, "
                        f"geom_not_null={'geom' in not_null}, "
                        f"primary_key={sorted(primary_key)})"
                    )
                cursor.execute(
                    sql.SQL("COMMENT ON TABLE {}.{} IS {}").format(
                        sql.Identifier(self.schema),
                        sql.Identifier(self.target_table),
                        sql.Literal(TABLE_COMMENT),
                    )
                )
                for column_name, comment in (
                    ("oa21cd", OA_CODE_COMMENT),
                    ("geom", GEOMETRY_COMMENT),
                    ("geom_3857", GEOMETRY_3857_COMMENT),
                ):
                    cursor.execute(
                        sql.SQL("COMMENT ON COLUMN {}.{}.{} IS {}").format(
                            sql.Identifier(self.schema),
                            sql.Identifier(self.target_table),
                            sql.Identifier(column_name),
                            sql.Literal(comment),
                        )
                    )
                cursor.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {} ON {}.{} "
                        "USING gist (geom_3857)"
                    ).format(
                        sql.Identifier(f"{self.target_table}_geom_3857_gix"),
                        sql.Identifier(self.schema),
                        sql.Identifier(self.target_table),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.{} (
                            dataset_key text PRIMARY KEY,
                            target_table text NOT NULL,
                            oa_count bigint NOT NULL,
                            variable_count integer NOT NULL,
                            geometry_repairs integer NOT NULL,
                            geometry_source_url text NOT NULL,
                            geometry_source_sha256 text NOT NULL,
                            source_metadata jsonb NOT NULL,
                            published_at timestamp with time zone NOT NULL
                                DEFAULT CURRENT_TIMESTAMP,
                            last_successful_run_id uuid NOT NULL
                        )
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(DATASET_METADATA_TABLE),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS "
                        "geometry_repairs integer NOT NULL DEFAULT 0"
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(DATASET_METADATA_TABLE),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.{} (
                            dataset_key text NOT NULL
                                REFERENCES {}.{} (dataset_key) ON DELETE CASCADE,
                            column_name text NOT NULL,
                            topic_id text NOT NULL,
                            topic_title text NOT NULL,
                            ordinal integer NOT NULL CHECK (ordinal > 0),
                            label text NOT NULL,
                            source_url text NOT NULL,
                            source_member text NOT NULL,
                            source_sha256 text NOT NULL,
                            source_metadata jsonb NOT NULL,
                            PRIMARY KEY (dataset_key, column_name),
                            UNIQUE (dataset_key, topic_id, ordinal)
                        )
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(VARIABLE_METADATA_TABLE),
                        sql.Identifier(self.schema),
                        sql.Identifier(DATASET_METADATA_TABLE),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.{} (
                            run_id uuid PRIMARY KEY,
                            dataset_key text NOT NULL,
                            target_table text NOT NULL,
                            status text NOT NULL
                                CHECK (status IN ('running', 'succeeded', 'failed')),
                            started_at timestamp with time zone NOT NULL
                                DEFAULT CURRENT_TIMESTAMP,
                            finished_at timestamp with time zone,
                            geometry_rows bigint NOT NULL DEFAULT 0,
                            geometry_repairs integer NOT NULL DEFAULT 0,
                            topics_loaded integer NOT NULL DEFAULT 0,
                            error text
                        )
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(RUN_TABLE),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS "
                        "geometry_repairs integer NOT NULL DEFAULT 0"
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(RUN_TABLE),
                    )
                )
                # Deliberately not underscore-prefixed, unlike RUN_TABLE:
                # semantic sync excludes "_"-prefixed relations from
                # discovery, and the federation verifier must be able to
                # read this record (see
                # docs/federation-architecture-waypoint.md, "Publication
                # record"). Exactly one row — the current release —
                # enforced by the boolean singleton primary key.
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.{} (
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
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(DATASET_PUBLICATION_TABLE),
                    )
                )

        self._commit_or_rollback(operation)

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

        Call this once, as the last step of a fully successful Census ETL
        cycle — after publish() has already committed — not per topic or per
        run. row_counts is computed from the stable target table inside this
        same transaction, so it can never disagree with what was actually
        just published; if anything raises, _commit_or_rollback rolls the
        whole write back and the previous release's record is left intact,
        per the federation architecture waypoint's atomic ETL boundary.
        """

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "SELECT schema_version, geometry_contract_version "
                        "FROM {}.{} WHERE singleton"
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(DATASET_PUBLICATION_TABLE),
                    )
                )
                previous = cursor.fetchone()
                if previous is not None:
                    previous_schema_version, previous_geometry_version = previous
                    if schema_version < previous_schema_version:
                        raise CensusDatabaseError(
                            "schema_version must not regress: "
                            f"{schema_version} < {previous_schema_version}"
                        )
                    if geometry_contract_version < previous_geometry_version:
                        raise CensusDatabaseError(
                            "geometry_contract_version must not regress: "
                            f"{geometry_contract_version} < "
                            f"{previous_geometry_version}"
                        )

                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(self.schema),
                        sql.Identifier(self.target_table),
                    )
                )
                row_counts = {self.target_table: cursor.fetchone()[0]}

                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.{}
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
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(DATASET_PUBLICATION_TABLE),
                    ),
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

    def start_run(self, run_id: uuid.UUID) -> None:
        def operation() -> None:
            with self.connection.cursor() as cursor:
                # run_census holds the dataset session advisory lock here, so
                # no live peer can be mistaken for an abandoned session.
                cursor.execute(
                    sql.SQL(
                        """
                        UPDATE {}.{}
                        SET status = 'failed',
                            finished_at = CURRENT_TIMESTAMP,
                            error = %s
                        WHERE dataset_key = %s
                          AND target_table = %s
                          AND status = 'running'
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(RUN_TABLE),
                    ),
                    (
                        ABANDONED_RUN_ERROR,
                        self.target_table,
                        self.target_table,
                    ),
                )
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.{}
                            (run_id, dataset_key, target_table, status)
                        VALUES (%s, %s, %s, 'running')
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(RUN_TABLE),
                    ),
                    (run_id, self.target_table, self.target_table),
                )

        self._commit_or_rollback(operation)

    @staticmethod
    def _wide_stage_name(run_id: uuid.UUID) -> str:
        return f"census_wide_{run_id.hex}"

    @staticmethod
    def _geometry_stage_name(run_id: uuid.UUID) -> str:
        return f"census_geometry_{run_id.hex}"

    @staticmethod
    def _topic_stage_name(run_id: uuid.UUID, topic_index: int) -> str:
        return f"census_topic_{topic_index:02d}_{run_id.hex}"

    def create_geometry_stage(self, run_id: uuid.UUID) -> str:
        stage = self._geometry_stage_name(run_id)
        geometry_definitions = [
            sql.SQL("{} {}").format(
                sql.Identifier(column.target),
                TYPE_SQL[column.postgres_type],
            )
            for column in self.geometry_columns
            if column.target != "oa21cd"
        ]

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "CREATE TEMP TABLE {} ({}) "
                        "ON COMMIT PRESERVE ROWS"
                    ).format(
                        sql.Identifier(stage),
                        sql.SQL(", ").join(
                            [
                                sql.SQL("oa21cd text PRIMARY KEY"),
                                *geometry_definitions,
                                sql.SQL(
                                    "geom geometry(MultiPolygon, 4326) NOT NULL"
                                ),
                            ]
                        ),
                    )
                )

        self._commit_or_rollback(operation)
        return stage

    def insert_geometry_page(
        self,
        stage: str,
        features: Sequence[PreparedFeature],
    ) -> None:
        if not features:
            return
        _require_identifier(stage, "staging table")
        column_names = [column.target for column in self.geometry_columns]
        insert_columns = [
            *(sql.Identifier(column) for column in column_names),
            sql.Identifier("geom"),
        ]
        placeholders: list[sql.Composable] = [
            sql.Placeholder() for _ in column_names
        ]
        placeholders.append(
            sql.SQL(
                "ST_Multi(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))"
            )
        )
        statement = sql.SQL(
            "INSERT INTO pg_temp.{} ({}) VALUES ({})"
        ).format(
            sql.Identifier(stage),
            sql.SQL(", ").join(insert_columns),
            sql.SQL(", ").join(placeholders),
        )
        rows = [
            (
                *feature.values,
                json.dumps(
                    feature.geometry,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            )
            for feature in features
        ]

        def operation() -> None:
            with self.connection.cursor() as cursor:
                try:
                    cursor.executemany(statement, rows)
                except errors.UniqueViolation as exc:
                    raise CensusDuplicateCodeError(
                        "geometry source contains a duplicate oa21cd"
                    ) from exc

        self._commit_or_rollback(operation)

    def validate_geometry(
        self,
        stage: str,
        expected_count: int,
        max_repairs: int,
    ) -> tuple[str, ...]:
        _require_identifier(stage, "staging table")
        if max_repairs < 0:
            raise CensusDatabaseError("max_repairs must not be negative")

        def operation() -> tuple[str, ...]:
            with self.connection.cursor() as cursor:
                def inspect() -> tuple[
                    int,
                    int,
                    int,
                    int,
                    int,
                    int,
                    tuple[str, ...],
                ]:
                    cursor.execute(
                        sql.SQL(
                            """
                            SELECT
                                count(*)::bigint,
                                count(*) FILTER (
                                    WHERE oa21cd !~ '^E[0-9]{{8}}$'
                                )::bigint,
                                count(*) FILTER (WHERE geom IS NULL)::bigint,
                                count(*) FILTER (
                                    WHERE geom IS NOT NULL
                                      AND ST_IsEmpty(geom)
                                )::bigint,
                                count(*) FILTER (
                                    WHERE geom IS NOT NULL AND (
                                        GeometryType(geom) <> 'MULTIPOLYGON'
                                        OR ST_SRID(geom) <> 4326
                                    )
                                )::bigint,
                                count(*) FILTER (
                                    WHERE geom IS NOT NULL
                                      AND NOT ST_IsValid(geom)
                                )::bigint,
                                COALESCE(
                                    array_agg(oa21cd ORDER BY oa21cd) FILTER (
                                        WHERE geom IS NOT NULL
                                          AND NOT ST_IsValid(geom)
                                    ),
                                    ARRAY[]::text[]
                                )
                            FROM pg_temp.{}
                            """
                        ).format(sql.Identifier(stage))
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise CensusDatabaseError(
                            "geometry validation returned no result"
                        )
                    counts = tuple(int(value) for value in row[:6])
                    raw_candidates = row[6]
                    if not isinstance(raw_candidates, (list, tuple)):
                        raise CensusDatabaseError(
                            "geometry repair candidates were not returned as "
                            "a PostgreSQL text array"
                        )
                    return (  # type: ignore[return-value]
                        *counts,
                        tuple(str(value) for value in raw_candidates),
                    )

                (
                    count,
                    invalid_codes,
                    null_geometries,
                    empty_geometries,
                    wrong_shape,
                    invalid_geometries,
                    repair_candidates,
                ) = inspect()
                if (
                    count != expected_count
                    or invalid_codes
                    or null_geometries
                    or empty_geometries
                    or wrong_shape
                ):
                    raise CensusDatabaseError(
                        "geometry staging validation failed before repair "
                        f"(expected={expected_count}, rows={count}, "
                        f"invalid_codes={invalid_codes}, "
                        f"null_geometries={null_geometries}, "
                        f"empty_geometries={empty_geometries}, "
                        f"wrong_type_or_srid={wrong_shape}, "
                        f"invalid_geometries={invalid_geometries})"
                    )
                if (
                    len(repair_candidates) != invalid_geometries
                    or repair_candidates
                    != tuple(sorted(set(repair_candidates)))
                ):
                    raise CensusDatabaseError(
                        "geometry repair candidate audit is inconsistent "
                        f"(invalid={invalid_geometries}, "
                        f"candidates={len(repair_candidates)})"
                    )
                if invalid_geometries > max_repairs:
                    raise CensusDatabaseError(
                        "geometry repair limit exceeded "
                        f"(invalid={invalid_geometries}, "
                        f"maximum={max_repairs}); no geometries were repaired"
                    )
                if invalid_geometries == 0:
                    return repair_candidates

                cursor.execute(
                    sql.SQL(
                        """
                        UPDATE pg_temp.{}
                        SET geom = ST_Multi(
                            ST_CollectionExtract(ST_MakeValid(geom), 3)
                        )
                        WHERE NOT ST_IsValid(geom)
                        """
                    ).format(sql.Identifier(stage))
                )
                if cursor.rowcount != invalid_geometries:
                    raise CensusDatabaseError(
                        "geometry repair row count changed unexpectedly "
                        f"(expected={invalid_geometries}, "
                        f"updated={cursor.rowcount})"
                    )

                (
                    repaired_count,
                    repaired_invalid_codes,
                    repaired_nulls,
                    repaired_empties,
                    repaired_wrong_shape,
                    remaining_invalid,
                    remaining_candidates,
                ) = inspect()
                if (
                    repaired_count != expected_count
                    or repaired_invalid_codes
                    or repaired_nulls
                    or repaired_empties
                    or repaired_wrong_shape
                    or remaining_invalid
                    or remaining_candidates
                ):
                    raise CensusDatabaseError(
                        "geometry repair did not produce valid non-empty "
                        "MultiPolygon geometry "
                        f"(expected={expected_count}, rows={repaired_count}, "
                        f"invalid_codes={repaired_invalid_codes}, "
                        f"null_geometries={repaired_nulls}, "
                        f"empty_geometries={repaired_empties}, "
                        f"wrong_type_or_srid={repaired_wrong_shape}, "
                        f"invalid_geometries={remaining_invalid}, "
                        f"repair_candidates={len(remaining_candidates)})"
                    )
                return repair_candidates

        return tuple(self._commit_or_rollback(operation))

    def create_topic_stage(
        self,
        run_id: uuid.UUID,
        topic_index: int,
        columns: Sequence[str],
    ) -> str:
        stage = self._topic_stage_name(run_id, topic_index)
        _require_identifier(stage, "topic staging table")
        definitions = [
            sql.SQL("{} double precision").format(
                sql.Identifier(_require_identifier(column, "topic column"))
            )
            for column in columns
        ]

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TEMP TABLE {} (
                            oa21cd text PRIMARY KEY,
                            {}
                        ) ON COMMIT PRESERVE ROWS
                        """
                    ).format(
                        sql.Identifier(stage),
                        sql.SQL(", ").join(definitions),
                    )
                )

        self._commit_or_rollback(operation)
        return stage

    def copy_topic_rows(
        self,
        topic_stage: str,
        columns: Sequence[str],
        rows: Iterable[tuple[Any, ...]],
    ) -> None:
        _require_identifier(topic_stage, "topic staging table")
        column_identifiers = [
            sql.Identifier("oa21cd"),
            *(
                sql.Identifier(_require_identifier(column, "topic column"))
                for column in columns
            ),
        ]
        statement = sql.SQL("COPY pg_temp.{} ({}) FROM STDIN").format(
            sql.Identifier(topic_stage),
            sql.SQL(", ").join(column_identifiers),
        )

        def operation() -> None:
            with self.connection.cursor() as cursor:
                try:
                    with cursor.copy(statement) as copy:
                        for row in rows:
                            copy.write_row(row)
                except errors.UniqueViolation as exc:
                    raise CensusDuplicateCodeError(
                        "Nomis topic contains a duplicate oa21cd"
                    ) from exc

        self._commit_or_rollback(operation)

    def apply_topic(
        self,
        geometry_stage: str,
        topic_stage: str,
        columns: Sequence[str],
        expected_count: int,
    ) -> None:
        _require_identifier(geometry_stage, "geometry staging table")
        _require_identifier(topic_stage, "topic staging table")
        for column in columns:
            _require_identifier(column, "topic column")

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        SELECT
                            (SELECT count(*) FROM pg_temp.{})::bigint,
                            (
                                SELECT count(*)
                                FROM pg_temp.{} AS wide
                                LEFT JOIN pg_temp.{} AS topic USING (oa21cd)
                                WHERE topic.oa21cd IS NULL
                            )::bigint,
                            (
                                SELECT count(*)
                                FROM pg_temp.{} AS topic
                                LEFT JOIN pg_temp.{} AS wide USING (oa21cd)
                                WHERE wide.oa21cd IS NULL
                            )::bigint
                        """
                    ).format(
                        sql.Identifier(topic_stage),
                        sql.Identifier(geometry_stage),
                        sql.Identifier(topic_stage),
                        sql.Identifier(topic_stage),
                        sql.Identifier(geometry_stage),
                    )
                )
                row = cursor.fetchone()
                if row is None:
                    raise CensusDatabaseError(
                        "topic code-set validation returned no result"
                    )
                count, missing, extra = (int(value) for value in row)
                if count != expected_count or missing or extra:
                    raise CensusCodeSetError(
                        "Nomis topic OA code set does not match geometry "
                        f"(expected={expected_count}, rows={count}, "
                        f"missing={missing}, extra={extra})"
                    )

        self._commit_or_rollback(operation)

    def assemble_wide_stage(
        self,
        run_id: uuid.UUID,
        geometry_stage: str,
        topic_stages: Sequence[tuple[str, Sequence[str]]],
        expected_count: int,
    ) -> str:
        """Join each narrow topic once and write the 467-column snapshot once."""

        wide_stage = self._wide_stage_name(run_id)
        _require_identifier(geometry_stage, "geometry staging table")
        _require_identifier(wide_stage, "wide staging table")
        select_columns: list[sql.Composable] = [
            sql.SQL("geometry.{}").format(sql.Identifier(column.target))
            for column in self.geometry_columns
        ]
        joins: list[sql.Composable] = []
        for index, (topic_stage, columns) in enumerate(topic_stages):
            _require_identifier(topic_stage, "topic staging table")
            alias = f"topic_{index}"
            joins.append(
                sql.SQL(
                    "JOIN pg_temp.{} AS {} USING (oa21cd)"
                ).format(
                    sql.Identifier(topic_stage),
                    sql.Identifier(alias),
                )
            )
            select_columns.extend(
                sql.SQL("{}.{}").format(
                    sql.Identifier(alias),
                    sql.Identifier(
                        _require_identifier(column, "topic column")
                    ),
                )
                for column in columns
            )
        select_columns.append(sql.SQL("geometry.geom"))
        data_columns = [
            *(column.target for column in self.geometry_columns),
            *self.statistic_columns,
            "geom",
        ]
        if len(select_columns) != len(data_columns):
            raise CensusDatabaseError(
                "topic staging columns do not match configured Census columns"
            )

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        CREATE TEMP TABLE {} (
                            LIKE {}.{} INCLUDING DEFAULTS INCLUDING GENERATED,
                            PRIMARY KEY (oa21cd)
                        ) ON COMMIT PRESERVE ROWS
                        """
                    ).format(
                        sql.Identifier(wide_stage),
                        sql.Identifier(self.schema),
                        sql.Identifier(self.target_table),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO pg_temp.{} ({})
                        SELECT {}
                        FROM pg_temp.{} AS geometry
                        {}
                        ORDER BY geometry.oa21cd
                        """
                    ).format(
                        sql.Identifier(wide_stage),
                        sql.SQL(", ").join(
                            sql.Identifier(column) for column in data_columns
                        ),
                        sql.SQL(", ").join(select_columns),
                        sql.Identifier(geometry_stage),
                        sql.SQL(" ").join(joins),
                    )
                )
                if cursor.rowcount != expected_count:
                    raise CensusDatabaseError(
                        "wide Census assembly returned an unexpected row count "
                        f"(expected={expected_count}, inserted={cursor.rowcount})"
                    )

        self._commit_or_rollback(operation)
        return wide_stage

    def cleanup_stages(self, stages: Sequence[str]) -> None:
        checked = [
            _require_identifier(stage, "staging table")
            for stage in dict.fromkeys(stages)
        ]
        if not checked:
            return

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}").format(
                        sql.SQL(", ").join(
                            sql.SQL("pg_temp.{}").format(sql.Identifier(stage))
                            for stage in checked
                        )
                    )
                )

        self._commit_or_rollback(operation)

    def record_progress(
        self,
        run_id: uuid.UUID,
        *,
        geometry_rows: int,
        geometry_repairs: int,
        topics_loaded: int,
    ) -> None:
        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        UPDATE {}.{}
                        SET geometry_rows = %s, geometry_repairs = %s,
                            topics_loaded = %s
                        WHERE run_id = %s AND status = 'running'
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(RUN_TABLE),
                    ),
                    (geometry_rows, geometry_repairs, topics_loaded, run_id),
                )

        self._commit_or_rollback(operation)

    def publish(
        self,
        stage: str,
        run_id: uuid.UUID,
        dataset: CensusDatasetMetadata,
        variables: Sequence[CensusVariableMetadata],
    ) -> None:
        """Atomically replace stable rows and publish their semantic metadata."""

        _require_identifier(stage, "staging table")
        data_columns = [
            *(column.target for column in self.geometry_columns),
            *self.statistic_columns,
            "geom",
        ]
        column_sql = sql.SQL(", ").join(
            sql.Identifier(column) for column in data_columns
        )
        variable_comments: list[tuple[str, str]] = []
        seen_variable_columns: set[str] = set()
        statistic_columns = set(self.statistic_columns)
        for variable in variables:
            column_name = _require_identifier(
                variable.column_name,
                "Census variable column",
            )
            if (
                column_name not in statistic_columns
                or column_name in seen_variable_columns
            ):
                raise CensusDatabaseError(
                    f"{column_name}: Census variable comment does not match "
                    "one unique statistic column"
                )
            seen_variable_columns.add(column_name)
            variable_comments.append(
                (column_name, _variable_comment(variable))
            )

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("TRUNCATE TABLE {}.{}").format(
                        sql.Identifier(self.schema),
                        sql.Identifier(self.target_table),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {}.{} ({}) "
                        "SELECT {} FROM pg_temp.{} ORDER BY oa21cd"
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(self.target_table),
                        column_sql,
                        column_sql,
                        sql.Identifier(stage),
                    )
                )
                if cursor.rowcount != dataset.oa_count:
                    raise CensusDatabaseError(
                        "stable Census insert returned an unexpected row count "
                        f"(expected={dataset.oa_count}, inserted={cursor.rowcount})"
                    )
                for column_name, comment in variable_comments:
                    cursor.execute(
                        sql.SQL("COMMENT ON COLUMN {}.{}.{} IS {}").format(
                            sql.Identifier(self.schema),
                            sql.Identifier(self.target_table),
                            sql.Identifier(column_name),
                            sql.Literal(comment),
                        )
                    )
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.{} (
                            dataset_key, target_table, oa_count, variable_count,
                            geometry_repairs,
                            geometry_source_url, geometry_source_sha256,
                            source_metadata, published_at,
                            last_successful_run_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                CURRENT_TIMESTAMP, %s)
                        ON CONFLICT (dataset_key) DO UPDATE SET
                            target_table = EXCLUDED.target_table,
                            oa_count = EXCLUDED.oa_count,
                            variable_count = EXCLUDED.variable_count,
                            geometry_repairs = EXCLUDED.geometry_repairs,
                            geometry_source_url =
                                EXCLUDED.geometry_source_url,
                            geometry_source_sha256 =
                                EXCLUDED.geometry_source_sha256,
                            source_metadata = EXCLUDED.source_metadata,
                            published_at = EXCLUDED.published_at,
                            last_successful_run_id =
                                EXCLUDED.last_successful_run_id
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(DATASET_METADATA_TABLE),
                    ),
                    (
                        dataset.dataset_key,
                        self.target_table,
                        dataset.oa_count,
                        dataset.variable_count,
                        dataset.geometry_repairs,
                        dataset.geometry_source_url,
                        dataset.geometry_source_sha256,
                        Jsonb(dataset.source_metadata),
                        run_id,
                    ),
                )
                cursor.execute(
                    sql.SQL(
                        "DELETE FROM {}.{} WHERE dataset_key = %s"
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(VARIABLE_METADATA_TABLE),
                    ),
                    (dataset.dataset_key,),
                )
                variable_statement = sql.SQL(
                    """
                    INSERT INTO {}.{} (
                        dataset_key, column_name, topic_id, topic_title,
                        ordinal, label, source_url, source_member,
                        source_sha256, source_metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                ).format(
                    sql.Identifier(self.schema),
                    sql.Identifier(VARIABLE_METADATA_TABLE),
                )
                cursor.executemany(
                    variable_statement,
                    [
                        (
                            dataset.dataset_key,
                            variable.column_name,
                            variable.topic_id,
                            variable.topic_title,
                            variable.ordinal,
                            variable.label,
                            variable.source_url,
                            variable.source_member,
                            variable.source_sha256,
                            Jsonb(variable.source_metadata),
                        )
                        for variable in variables
                    ],
                )
                cursor.execute(
                    sql.SQL(
                        """
                        UPDATE {}.{}
                        SET status = 'succeeded',
                            finished_at = CURRENT_TIMESTAMP,
                            geometry_rows = %s,
                            geometry_repairs = %s,
                            topics_loaded = %s,
                            error = NULL
                        WHERE run_id = %s AND status = 'running'
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(RUN_TABLE),
                    ),
                    (
                        dataset.oa_count,
                        dataset.geometry_repairs,
                        len({variable.topic_id for variable in variables}),
                        run_id,
                    ),
                )
                cursor.execute(
                    sql.SQL("DROP TABLE pg_temp.{}").format(
                        sql.Identifier(stage)
                    )
                )

        self._commit_or_rollback(operation)

    def fail_run(self, run_id: uuid.UUID, error: str) -> None:
        message = error[:10_000]

        def operation() -> None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        UPDATE {}.{}
                        SET status = 'failed',
                            finished_at = CURRENT_TIMESTAMP,
                            error = %s
                        WHERE run_id = %s AND status = 'running'
                        """
                    ).format(
                        sql.Identifier(self.schema),
                        sql.Identifier(RUN_TABLE),
                    ),
                    (message, run_id),
                )

        self._commit_or_rollback(operation)

    def cleanup_stage(self, stage: str) -> None:
        self.cleanup_stages((stage,))
