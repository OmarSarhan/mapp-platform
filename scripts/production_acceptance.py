from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from validate_production_env import assignments, validate
except ModuleNotFoundError:  # Imported as scripts.production_acceptance in tests.
    from scripts.validate_production_env import assignments, validate


ROOT = Path(__file__).resolve().parents[1]
# Modes where MAPP runs its own PostgreSQL, so the bundled compose file and
# the db service apply. External points at a server MAPP does not run.
DEFAULT_OUTPUT = ROOT / "var" / "acceptance" / "production-evidence.json"


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    summary: str
    reason: str | None = None


def check(check_id: str, status: str, summary: str, reason: str | None = None) -> Check:
    return Check(check_id, status, summary, reason)


def run_quiet(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def environment_checks(environment: Path) -> tuple[dict[str, str], list[Check]]:
    if not environment.is_file():
        return {}, [check("environment.file", "fail", "Production environment file is missing.")]
    mode = stat.S_IMODE(environment.stat().st_mode)
    mode_check = (
        check("environment.permissions", "pass", "Environment file is owner-readable only.")
        if mode & 0o077 == 0
        else check(
            "environment.permissions",
            "fail",
            "Environment file permissions expose it beyond its owner.",
        )
    )
    values = assignments(environment)
    errors = validate(values)
    validation_check = (
        check("environment.production", "pass", "Production settings passed validation.")
        if not errors
        else check(
            "environment.production",
            "fail",
            "Production settings failed validation.",
            f"{len(errors)} validation error(s); run scripts/validate_production_env.py for details.",
        )
    )
    mode_value = values.get("MAPP_ENVIRONMENT", "")
    topology_check = (
        check("environment.topology", "pass", "Production topology is selected.")
        if mode_value == "production"
        else check(
            "environment.topology",
            "fail",
            "Production topology is not selected.",
            "MAPP_ENVIRONMENT must be production in the reviewed environment file.",
        )
    )
    return values, [mode_check, validation_check, topology_check]


def compose_command(environment: Path, values: dict[str, str]) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(environment),
        "--file",
        str(ROOT / "compose.yaml"),
    ]
    command += ["--file", str(ROOT / "compose.bundled-db.yaml")]
    command += ["--file", str(ROOT / "compose.production.yaml")]
    return command


def docker_checks(environment: Path, values: dict[str, str], live: bool) -> list[Check]:
    if shutil.which("docker") is None:
        return [
            check(
                "compose.config",
                "pending",
                "Compose configuration was not validated.",
                "Docker is unavailable in this environment.",
            ),
            check(
                "services.health",
                "pending",
                "Container health was not observed.",
                "Docker is unavailable in this environment.",
            ),
        ]
    version = run_quiet(["docker", "compose", "version"], 20)
    if version.returncode:
        return [
            check("compose.config", "fail", "Docker Compose v2 is unavailable."),
            check(
                "services.health",
                "pending",
                "Container health was not observed.",
                "Compose configuration could not be constructed.",
            ),
        ]
    command = compose_command(environment, values)
    configured = run_quiet(command + ["config", "--quiet"])
    checks = [
        check("compose.config", "pass", "Production Compose model is valid.")
        if configured.returncode == 0
        else check(
            "compose.config",
            "fail",
            "Production Compose model is invalid.",
            "Run ./bin/mapp config for the redacted diagnostic.",
        )
    ]
    if not live:
        checks.append(
            check(
                "services.health",
                "pending",
                "Container health was not observed.",
                "Run with --live on the production host after services are started.",
            )
        )
        return checks
    services = [
        "semantic-service", "xyz", "xyz-preview", "config-ui",
        "browser-runner", "egress-proxy", "caddy",
    ]
    services.insert(0, "db")
    ps = run_quiet(command + ["ps", "--format", "json"], 30)
    if ps.returncode:
        checks.append(check("services.health", "fail", "Service state could not be read."))
        return checks
    try:
        rows = [json.loads(line) for line in ps.stdout.decode().splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        checks.append(check("services.health", "fail", "Service state was not valid JSON."))
        return checks
    states = {row.get("Service"): row.get("Health") or row.get("State") for row in rows}
    unhealthy = [service for service in services if states.get(service) != "healthy"]
    checks.append(
        check("services.health", "pass", "All required containers report healthy.")
        if not unhealthy
        else check(
            "services.health",
            "fail",
            "Required containers are not all healthy.",
            "Affected service names: " + ", ".join(unhealthy),
        )
    )
    return checks


def public_checks(values: dict[str, str], live: bool) -> list[Check]:
    results: list[Check] = []
    for label, key in (("map", "PRODUCTION_MAP_SITE"), ("config", "PRODUCTION_CONFIG_SITE")):
        origin = values.get(key, "")
        hostname = urlsplit(origin).hostname
        if not live:
            results.extend(
                [
                    check(
                        f"dns.{label}",
                        "pending",
                        f"{label.title()} DNS was not queried.",
                        "Run with --live from a network that should reach production.",
                    ),
                    check(
                        f"tls.{label}",
                        "pending",
                        f"{label.title()} TLS was not inspected.",
                        "Run with --live after public DNS and ACME issuance are active.",
                    ),
                    check(
                        f"http.{label}",
                        "pending",
                        f"{label.title()} public health was not requested.",
                        "Run with --live after the production service is started.",
                    ),
                ]
            )
            continue
        if not hostname:
            results.extend(
                [
                    check(f"dns.{label}", "fail", f"{label.title()} hostname is unavailable."),
                    check(f"tls.{label}", "fail", f"{label.title()} TLS target is unavailable."),
                    check(f"http.{label}", "fail", f"{label.title()} public origin is unavailable."),
                ]
            )
            continue
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443)}
            results.append(
                check(f"dns.{label}", "pass", f"{label.title()} DNS resolved to {len(addresses)} address(es).")
            )
        except OSError as exc:
            results.append(check(f"dns.{label}", "fail", f"{label.title()} DNS did not resolve.", type(exc).__name__))
        try:
            context = ssl.create_default_context()
            with socket.create_connection((hostname, 443), timeout=10) as raw:
                with context.wrap_socket(raw, server_hostname=hostname) as secure:
                    certificate = secure.getpeercert()
            expiry = certificate.get("notAfter")
            results.append(
                check(f"tls.{label}", "pass", f"{label.title()} TLS chain and hostname verified.", f"Certificate notAfter: {expiry}")
            )
        except (OSError, ssl.SSLError) as exc:
            results.append(check(f"tls.{label}", "fail", f"{label.title()} TLS verification failed.", type(exc).__name__))
        health_url = origin.rstrip("/") + ("/healthz" if label == "config" else "/")
        try:
            request = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
            results.append(
                check(f"http.{label}", "pass", f"{label.title()} public endpoint returned HTTP {status}.")
                if status < 500
                else check(f"http.{label}", "fail", f"{label.title()} public endpoint returned HTTP {status}.")
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            results.append(check(f"http.{label}", "fail", f"{label.title()} public endpoint request failed.", type(exc).__name__))
    return results


def host_checks(live: bool) -> list[Check]:
    if not live:
        return [
            check(
                "host.firewall",
                "pending",
                "Host firewall policy was not observed.",
                "Run with --live on the production host; provider security groups still require separate review.",
            )
        ]
    firewall_tools = [tool for tool in ("ufw", "firewall-cmd", "nft") if shutil.which(tool)]
    if not firewall_tools:
        return [
            check(
                "host.firewall",
                "pending",
                "No supported host firewall tool was found.",
                "Cloud firewall/security-group policy is not observable from this process.",
            )
        ]
    tool = firewall_tools[0]
    argv = {"ufw": ["ufw", "status"], "firewall-cmd": ["firewall-cmd", "--list-all"], "nft": ["nft", "list", "ruleset"]}[tool]
    result = run_quiet(argv, 30)
    if result.returncode:
        return [
            check(
                "host.firewall",
                "pending",
                f"{tool} policy could not be read.",
                "Run acceptance with sufficient read access and review cloud controls separately.",
            )
        ]
    return [
        check(
            "host.firewall",
            "pass",
            f"{tool} exposed a readable host policy.",
            "This confirms observability only; an operator must review allowed ingress and provider controls.",
        )
    ]


def rehearsal_check(identifier: str, executable: Path | None, run: bool) -> Check:
    title = identifier.replace(".", " ")
    if executable is None:
        return check(
            identifier,
            "pending",
            f"{title.title()} was not rehearsed.",
            "Provide its explicit executable hook and --run-rehearsals on an isolated target.",
        )
    if not run:
        return check(
            identifier,
            "pending",
            f"{title.title()} hook was supplied but not executed.",
            "Add --run-rehearsals after confirming the hook targets an isolated environment.",
        )
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return check(identifier, "fail", f"{title.title()} hook is not an executable file.")
    try:
        result = run_quiet([str(executable.resolve())], 1800)
    except subprocess.TimeoutExpired:
        return check(identifier, "fail", f"{title.title()} hook exceeded 30 minutes.")
    digest = hashlib.sha256(result.stdout + result.stderr).hexdigest()
    return (
        check(identifier, "pass", f"{title.title()} hook completed successfully.", f"Captured-output SHA-256: {digest}")
        if result.returncode == 0
        else check(identifier, "fail", f"{title.title()} hook failed.", f"Exit status {result.returncode}; captured-output SHA-256: {digest}")
    )


def write_evidence(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".production-evidence-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect redacted production acceptance evidence.")
    parser.add_argument("--environment", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true", help="Perform public network, host, and running-service checks.")
    parser.add_argument("--run-rehearsals", action="store_true", help="Execute explicitly supplied isolated rehearsal hooks.")
    parser.add_argument("--backup-hook", type=Path)
    parser.add_argument("--restore-hook", type=Path)
    parser.add_argument("--upgrade-hook", type=Path)
    parser.add_argument("--rollback-hook", type=Path)
    parser.add_argument("--require-complete", action="store_true", help="Return 3 when any evidence remains pending.")
    args = parser.parse_args()

    values, checks = environment_checks(args.environment)
    if values:
        checks += docker_checks(args.environment, values, args.live)
        checks += public_checks(values, args.live)
    else:
        checks += [
            check("compose.config", "pending", "Compose configuration was not validated.", "Environment file is unavailable."),
            check("services.health", "pending", "Container health was not observed.", "Environment file is unavailable."),
        ]
    checks += host_checks(args.live)
    checks += [
        rehearsal_check("backup.create", args.backup_hook, args.run_rehearsals),
        rehearsal_check("backup.isolated_restore", args.restore_hook, args.run_rehearsals),
        rehearsal_check("release.upgrade", args.upgrade_hook, args.run_rehearsals),
        rehearsal_check("release.rollback", args.rollback_hook, args.run_rehearsals),
    ]
    counts = {status: sum(item.status == status for item in checks) for status in ("pass", "fail", "pending")}
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "environmentFile": str(args.environment.resolve()),
        "liveChecksRequested": args.live,
        "rehearsalsRequested": args.run_rehearsals,
        "summary": counts,
        "checks": [asdict(item) for item in checks],
    }
    write_evidence(args.output, payload)
    print(f"Production acceptance evidence written to {args.output}.")
    print(f"pass={counts['pass']} fail={counts['fail']} pending={counts['pending']}")
    if counts["fail"]:
        return 1
    if args.require_complete and counts["pending"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
