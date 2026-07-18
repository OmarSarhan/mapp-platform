#!/usr/bin/env python3
"""Validate an external PostgreSQL URI without exposing its credentials."""

from __future__ import annotations

import ipaddress
import re
import socket
import sys
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit


class DatabaseUrlError(ValueError):
    """Raised when a runtime database URI is unsafe for external mode."""


DNS_LABEL = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.ASCII,
)


def effective_hostname(value: str) -> str:
    try:
        decoded = unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatabaseUrlError("DBS_MAPP contains an invalid encoded hostname") from exc
    if "%" in decoded or any(ord(character) < 33 for character in decoded):
        raise DatabaseUrlError("DBS_MAPP contains an invalid encoded hostname")
    candidate = decoded.lower().rstrip(".")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DatabaseUrlError("DBS_MAPP contains an invalid hostname") from exc


def validate_external_database_url(value: str) -> None:
    if not value:
        raise DatabaseUrlError("DBS_MAPP must be set for external mode")

    try:
        parsed = urlsplit(value)
        raw_host = parsed.hostname
    except ValueError as exc:
        raise DatabaseUrlError("DBS_MAPP is not a valid PostgreSQL URI") from exc

    if parsed.scheme not in {"postgres", "postgresql"} or not raw_host:
        raise DatabaseUrlError("DBS_MAPP must be a PostgreSQL URI with a hostname")
    if "#" in value:
        raise DatabaseUrlError("DBS_MAPP must not contain a fragment")
    override_keys = {
        key.lower()
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    forbidden_overrides = override_keys & {"host", "hostaddr", "service", "servicefile"}
    if forbidden_overrides:
        raise DatabaseUrlError(
            "external DBS_MAPP must not override its hostname through connection parameters"
        )

    normalized_host = effective_hostname(raw_host)
    if normalized_host == "db":
        raise DatabaseUrlError(
            "external DBS_MAPP must not use the bundled Compose hostname 'db'"
        )
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise DatabaseUrlError(
            "external DBS_MAPP must use a host reachable from the containers, not localhost"
        )

    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            socket.inet_aton(normalized_host)
        except OSError:
            labels = normalized_host.split(".")
            if (
                not normalized_host
                or len(normalized_host) > 253
                or any(DNS_LABEL.fullmatch(label) is None for label in labels)
            ):
                raise DatabaseUrlError("DBS_MAPP contains an invalid hostname")
            return
        raise DatabaseUrlError(
            "external DBS_MAPP must not use an ambiguous numeric hostname"
        )
    if address.is_loopback or address.is_unspecified:
        raise DatabaseUrlError(
            "external DBS_MAPP must use a non-loopback, container-reachable host"
        )


def main() -> int:
    value = sys.stdin.read().strip()
    try:
        validate_external_database_url(value)
    except DatabaseUrlError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
