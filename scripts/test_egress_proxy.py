#!/usr/bin/env python3
"""Exercise the HTTPS-only Squid allowlist with allowed and denied hosts."""

from __future__ import annotations

import argparse
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORIGIN_IMAGE = (
    "caddy:2.10.0-alpine@sha256:"
    "ae4458638da8e1a91aafffb231c5f8778e964bca650c8a8cb23a7e8ac557aa3c"
)


def command(*args: str, capture: bool = False) -> str:
    completed = subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    return completed.stdout.strip() if capture else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--proxy-image",
        default="mapp-egress-proxy:local",
        help="Already-built proxy image to exercise.",
    )
    parser.add_argument("--origin-image", default=DEFAULT_ORIGIN_IMAGE)
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:10]
    network = f"mapp-egress-test-{suffix}"
    origin = f"mapp-egress-origin-{suffix}"
    proxy = f"mapp-egress-proxy-{suffix}"
    created_containers: list[str] = []
    network_created = False
    failure_logs = ""

    try:
        command("docker", "network", "create", network)
        network_created = True
        subnet = command(
            "docker",
            "network",
            "inspect",
            network,
            "--format",
            "{{(index .IPAM.Config 0).Subnet}}",
            capture=True,
        )

        with tempfile.TemporaryDirectory(prefix="mapp-egress-test-") as directory:
            fixture = Path(directory)
            (fixture / "index.html").write_text("allowed\n", encoding="utf-8")
            (fixture / "allowlist.txt").write_text(
                "allowed.test\n",
                encoding="utf-8",
            )
            (fixture / "Caddyfile").write_text(
                "{\n"
                "    admin off\n"
                "}\n"
                "https://allowed.test, https://denied.test {\n"
                "    tls internal\n"
                "    root * /srv\n"
                "    file_server\n"
                "}\n",
                encoding="utf-8",
            )
            production_config = (
                ROOT / "docker/egress-proxy/squid.conf"
            ).read_text(encoding="utf-8")
            guard = "http_access deny blocked_destinations\n"
            if production_config.count(guard) != 1:
                raise RuntimeError("Expected one private-destination guard.")
            test_rule = (
                f"acl integration_origin dst {subnet}\n"
                "http_access allow allowed_destinations integration_origin\n"
            )
            (fixture / "squid.conf").write_text(
                production_config.replace(guard, test_rule + guard),
                encoding="utf-8",
            )

            command(
                "docker", "run", "--detach",
                "--name", origin,
                "--network", network,
                "--network-alias", "allowed.test",
                "--network-alias", "denied.test",
                "--volume", f"{fixture}:/srv:ro",
                "--volume", f"{fixture / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
                args.origin_image,
                "caddy", "run", "--config", "/etc/caddy/Caddyfile",
                "--adapter", "caddyfile",
            )
            created_containers.append(origin)

            command(
                "docker", "run", "--detach",
                "--name", proxy,
                "--network", network,
                "--publish", "127.0.0.1::3128",
                "--read-only",
                "--user", "13:13",
                "--entrypoint", "/usr/sbin/squid",
                "--tmpfs", "/run:size=1m,mode=0755,uid=13,gid=13",
                "--tmpfs", "/tmp:size=8m,mode=1777",
                "--tmpfs", "/var/log/squid:size=8m,mode=0750,uid=13,gid=13",
                "--tmpfs", "/var/spool/squid:size=8m,mode=0750,uid=13,gid=13",
                "--volume", f"{fixture / 'squid.conf'}:/etc/squid/squid.conf:ro",
                "--volume", (
                    f"{fixture / 'allowlist.txt'}:"
                    "/etc/squid/browser-egress-allowlist.txt:ro"
                ),
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                args.proxy_image,
                "-f", "/etc/squid/squid.conf", "-NYC",
            )
            created_containers.append(proxy)
            binding = command(
                "docker", "port", proxy, "3128/tcp", capture=True
            )
            proxy_port = int(binding.rsplit(":", 1)[1])
            proxy_url = f"http://127.0.0.1:{proxy_port}"
            tls_context = ssl.create_default_context()
            tls_context.check_hostname = False
            tls_context.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": proxy_url}),
                urllib.request.HTTPSHandler(context=tls_context),
            )

            deadline = time.monotonic() + 20
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    with opener.open("https://allowed.test/", timeout=2) as response:
                        body = response.read().decode("utf-8")
                        if response.status != 200 or body != "allowed\n":
                            raise RuntimeError(
                                f"Unexpected allowed response: {response.status} {body!r}"
                            )
                    break
                except (OSError, urllib.error.URLError) as error:
                    last_error = error
                    time.sleep(0.25)
            else:
                raise RuntimeError(
                    f"Allowed destination did not succeed: {last_error}"
                )

            try:
                opener.open("https://denied.test/", timeout=3)
            except urllib.error.HTTPError as error:
                if error.code != 403:
                    raise RuntimeError(
                        f"Denied destination returned HTTP {error.code}, expected 403."
                    ) from error
            except urllib.error.URLError as error:
                if "403" not in str(error.reason):
                    raise RuntimeError(
                        "Denied CONNECT did not return the expected 403."
                    ) from error
            else:
                raise RuntimeError("Denied destination unexpectedly succeeded.")

            http_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url})
            )
            try:
                http_opener.open("http://allowed.test/", timeout=3)
            except urllib.error.HTTPError as error:
                if error.code != 403:
                    raise RuntimeError(
                        f"Plain HTTP returned {error.code}, expected 403."
                    ) from error
            else:
                raise RuntimeError("Plain HTTP egress unexpectedly succeeded.")

            print(
                "PASS: allowed HTTPS returned 200; denied hostname and plain "
                "HTTP returned 403."
            )
        return 0
    except Exception:
        if proxy in created_containers:
            logged = subprocess.run(
                ("docker", "logs", proxy),
                check=False,
                capture_output=True,
                text=True,
            )
            failure_logs = "\n".join(
                (logged.stdout + logged.stderr).splitlines()[-100:]
            )
        raise
    finally:
        for container in reversed(created_containers):
            subprocess.run(
                ("docker", "container", "rm", "--force", container),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if network_created:
            subprocess.run(
                ("docker", "network", "rm", network),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if failure_logs:
            print(failure_logs)


if __name__ == "__main__":
    raise SystemExit(main())
