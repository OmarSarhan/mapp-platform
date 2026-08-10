"""Shared parsing for relation identity strings.

Six call sites across app.py, control_api.py, and derived_layers.py used to
re-implement the same "schema.relation" splitting independently, each
defaulting a missing schema to "public" (or, in derived_layers.py, requiring
one) slightly differently. This is the one place that logic lives now, so a
future alias/source dimension only has to be threaded through here.
"""

from __future__ import annotations

import re

# Must match IDENTIFIER_PART_RE in derived_layers.py — one identifier-part
# grammar, kept in two places deliberately (derived_layers.py predates this
# module and importing it here would pull in its full psycopg dependency
# chain for one regex), so do not let the two drift.
IDENTIFIER_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_relation(
    value: object,
    *,
    alias: str | None,
    default_schema: str | None = None,
    part_pattern: re.Pattern[str] | None = None,
) -> tuple[str | None, str, str] | None:
    """Parse a relation string into (alias, schema, relation).

    `value` may be a bare relation name, in which case the schema defaults to
    `default_schema` if one is given, or a dot-qualified "schema.relation"
    string. Returns None if `value` is not a non-empty string, contains more
    than one dot, has an empty schema or relation part, omits the schema
    while `default_schema` is None (the caller requires an already-qualified
    value), or either part fails to fullmatch `part_pattern` when one is
    given (callers that need identifier-shaped parts, not just non-empty
    ones, pass this rather than relying on the loose default).
    """
    if not isinstance(value, str):
        return None
    relation = value.strip()
    if not relation or relation.count(".") > 1:
        return None
    if "." in relation:
        schema, table = relation.split(".", 1)
    elif default_schema is not None:
        schema, table = default_schema, relation
    else:
        return None
    schema = schema.strip()
    table = table.strip()
    if not schema or not table:
        return None
    if part_pattern is not None and not (
        part_pattern.fullmatch(schema) and part_pattern.fullmatch(table)
    ):
        return None
    return alias, schema, table
