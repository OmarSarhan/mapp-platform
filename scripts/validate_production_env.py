from __future__ import annotations

import argparse
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


PRODUCTION_KEYS = (
    "PRODUCTION_MAP_SITE",
    "PRODUCTION_CONFIG_SITE",
    "PRODUCTION_CONFIG_ALLOWED_HOSTS",
    "PRODUCTION_CADDY_EMAIL",
    "EDGE_BIND_ADDRESS",
    "HTTP_PORT",
    "HTTPS_PORT",
    "CONFIG_UID",
    "CONFIG_GID",
)
ENV_OVERRIDE_KEYS = PRODUCTION_KEYS
RESERVED_SUFFIXES = (
    "alt",
    "arpa",
    "example",
    "example.com",
    "example.net",
    "example.org",
    "internal",
    "invalid",
    "local",
    "localhost",
    "onion",
    "test",
)
DNS_LABEL = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.ASCII,
)


def reserved_domain(hostname: str) -> bool:
    return any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in RESERVED_SUFFIXES
    )


def canonical_dns_hostname(hostname: str) -> str | None:
    candidate = hostname.lower()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if (
        not candidate
        or len(candidate) > 253
        or all(character in "0123456789." for character in candidate)
    ):
        return None
    labels = candidate.split(".")
    if any(not DNS_LABEL.fullmatch(label) for label in labels):
        return None
    return candidate


def assignments(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    for key in ENV_OVERRIDE_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def validated_origin(value: str, key: str, errors: list[str]) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        errors.append(f"{key} must be a valid HTTPS origin.")
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        errors.append(f"{key} must be an HTTPS origin without credentials, path, query, or fragment.")
        return None
    if port not in (None, 443):
        errors.append(f"{key} must use the standard HTTPS port 443.")
        return None
    if parsed.hostname.endswith("."):
        errors.append(f"{key} must not use a trailing-dot hostname.")
        return None
    hostname = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        invalid_host = True
    else:
        hostname = canonical_dns_hostname(hostname)
        invalid_host = (
            hostname is None
            or "." not in hostname
            or reserved_domain(hostname)
        )
    if invalid_host:
        errors.append(f"{key} must use a public, non-reserved DNS hostname.")
        return None
    return f"https://{hostname}"


def validate(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    missing = [key for key in PRODUCTION_KEYS if not values.get(key, "").strip()]
    if missing:
        errors.extend(f"{key} is required for production." for key in missing)
        return errors
    if values["HTTP_PORT"].strip() != "80":
        errors.append(
            "HTTP_PORT must be 80 so Caddy can redirect HTTP and complete standard ACME challenges."
        )
    if values["HTTPS_PORT"].strip() != "443":
        errors.append(
            "HTTPS_PORT must be 443 for the direct production deployment."
        )
    for key in ("CONFIG_UID", "CONFIG_GID"):
        if re.fullmatch(r"[1-9][0-9]*", values[key].strip()) is None:
            errors.append(
                f"{key} must be a positive non-root numeric ID for production."
            )

    raw_bind_address = values["EDGE_BIND_ADDRESS"].strip()
    try:
        bind_address = ipaddress.ip_address(raw_bind_address.strip("[]"))
    except ValueError:
        errors.append("EDGE_BIND_ADDRESS must be a valid host interface address.")
    else:
        if bind_address.is_loopback:
            errors.append(
                "EDGE_BIND_ADDRESS must not be loopback for the direct production deployment."
            )

    map_origin = validated_origin(
        values["PRODUCTION_MAP_SITE"].strip(),
        "PRODUCTION_MAP_SITE",
        errors,
    )
    config_origin = validated_origin(
        values["PRODUCTION_CONFIG_SITE"].strip(),
        "PRODUCTION_CONFIG_SITE",
        errors,
    )
    map_hostname = urlsplit(map_origin).hostname if map_origin else None
    config_hostname = urlsplit(config_origin).hostname if config_origin else None
    if map_hostname and config_hostname and map_hostname == config_hostname:
        errors.append(
            "PRODUCTION_MAP_SITE and PRODUCTION_CONFIG_SITE must use distinct hostnames."
        )

    raw_allowed_hosts = [
        item.strip()
        for item in values["PRODUCTION_CONFIG_ALLOWED_HOSTS"].split(",")
        if item.strip()
    ]
    if any(item.endswith(".") for item in raw_allowed_hosts):
        errors.append(
            "PRODUCTION_CONFIG_ALLOWED_HOSTS must not contain trailing-dot hostnames."
        )
    allowed_hosts = {item.lower() for item in raw_allowed_hosts}
    if "*" in allowed_hosts:
        errors.append("PRODUCTION_CONFIG_ALLOWED_HOSTS must not contain a wildcard.")
    if config_hostname and config_hostname not in allowed_hosts:
        errors.append(
            "PRODUCTION_CONFIG_ALLOWED_HOSTS must include the configuration hostname."
        )

    email = values["PRODUCTION_CADDY_EMAIL"].strip()
    email_domain = email.rsplit("@", 1)[-1].lower().rstrip(".")
    if (
        not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        or reserved_domain(email_domain)
    ):
        errors.append("PRODUCTION_CADDY_EMAIL must be a monitored, non-placeholder address.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate production-only public settings without printing their values."
    )
    parser.add_argument("--environment", type=Path, required=True)
    args = parser.parse_args()
    if not args.environment.is_file():
        print("Production environment file does not exist.")
        return 2
    errors = validate(assignments(args.environment))
    if errors:
        print("Production environment is invalid:")
        for error in errors:
            print(f"  {error}")
        return 1
    print("Production environment settings are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
