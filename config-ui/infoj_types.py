import json


def info_value_error(entry_type: str, pg_type: str, value) -> str | None:
    entry_type = entry_type or "text"
    numeric_types = {
        "smallint", "integer", "bigint", "numeric", "real",
        "double precision", "decimal",
    }
    if value is None:
        return None
    if entry_type in {"numeric", "integer"} and pg_type not in numeric_types:
        return f"XYZ {entry_type} entries require a numeric result; PostgreSQL returned {pg_type}."
    if entry_type == "boolean" and pg_type != "boolean":
        return f"XYZ boolean entries require boolean; PostgreSQL returned {pg_type}."
    if entry_type in {"json", "dataview"} and not isinstance(value, (dict, list)):
        return f"XYZ {entry_type} entries require a JSON object or array."
    if entry_type in {"pills", "images", "documents"} and not isinstance(value, list):
        return f"XYZ {entry_type} entries require an array."
    if entry_type == "pin" and not (
        isinstance(value, list) and len(value) == 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
    ):
        return "XYZ pin entries require an array containing exactly two numeric coordinates."
    if entry_type == "geometry":
        candidate = value
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                return "XYZ geometry entries require valid GeoJSON."
        if not isinstance(candidate, dict) or not isinstance(candidate.get("type"), str):
            return "XYZ geometry entries require a GeoJSON object with a type."
    if entry_type in {"text", "textarea", "html", "link", "image", "date", "datetime", "time"} and isinstance(value, (dict, list)):
        return f"XYZ {entry_type} entries require a scalar result, not a JSON object or array."
    return None
