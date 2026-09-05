"""M3 evidence: the two listeners, the Unix socket, and their disjoint routes.

The property this file exists to prove is structural rather than configured:
an internal path is a 404 on the edge listener because the edge listener's
route table does not contain it, not because Caddy declines to forward it.
Removing a path from the Caddyfile and removing it from the route table are
independent controls, and a reviewer should be able to see both hold.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import email
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from issuer import MappAuthorizationServer  # noqa: E402
from models import Client  # noqa: E402
from server import ControlServer  # noqa: E402
from server import EdgeServer  # noqa: E402
from server import EdgeUnixServer  # noqa: E402
import server  # noqa: E402
from server import client_address  # noqa: E402
from stub_store import StubStore  # noqa: E402
from unix_server import SocketPathInUse  # noqa: E402
from unix_server import SocketPathNotASocket  # noqa: E402
from unix_server import clear_stale_socket  # noqa: E402

ISSUER = "http://mcp.localhost"


class UnixConnection(http.client.HTTPConnection):
    """http.client over AF_UNIX, so the socket is driven exactly as Caddy will."""

    def __init__(self, path: str, timeout: int = 5) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def build_server_pair():
    store = StubStore()
    store.add_client(
        Client(
            client_id="mcp-client",
            name="Claude Code",
            redirect_uris=("http://127.0.0.1:9/callback",),
            scopes=("mcp:connect", "inspect"),
            token_endpoint_auth_method="none",
        )
    )
    return MappAuthorizationServer(
        store,
        issuer=ISSUER,
        resource=ISSUER + "/mcp",
        scopes_supported=("mcp:connect", "inspect"),
        admin_password_hash="",
        secure_cookies=False,
    )


class RouteIsolationTests(unittest.TestCase):
    """The edge and control surfaces must not contain each other's routes."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

    def setUp(self) -> None:
        authorization = build_server_pair()
        self.edge = EdgeServer(("127.0.0.1", 0), authorization)
        self.control = ControlServer(("127.0.0.1", 0), authorization)
        for server in (self.edge, self.control):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

    def tearDown(self) -> None:
        for server in (self.edge, self.control):
            server.shutdown()
            server.server_close()

    def _status(self, server, method: str, path: str) -> int:
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        try:
            body = None
            headers = {}
            if method == "POST":
                body = ""
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                headers["Content-Length"] = "0"
            connection.request(method, path, body=body, headers=headers)
            return connection.getresponse().status
        finally:
            connection.close()

    def test_the_two_route_tables_are_disjoint(self) -> None:
        self.assertTrue(set(self.edge.routes).isdisjoint(set(self.control.routes)))

    def test_every_control_route_is_absent_from_the_edge_listener(self) -> None:
        self.assertTrue(self.control.routes, "control table must not be empty")
        for method, path in self.control.routes:
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(404, self._status(self.edge, method, path))

    def test_every_edge_route_is_absent_from_the_control_listener(self) -> None:
        self.assertTrue(self.edge.routes, "edge table must not be empty")
        for method, path in self.edge.routes:
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(404, self._status(self.control, method, path))

    def test_control_paths_404_on_the_edge_for_every_method(self) -> None:
        for _, path in self.control.routes:
            for method in ("GET", "POST"):
                with self.subTest(route=f"{method} {path}"):
                    self.assertEqual(404, self._status(self.edge, method, path))


class UnixSocketTests(unittest.TestCase):
    """The deployed edge listener: an AF_UNIX socket Caddy connects to."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "mapp-auth.sock")
        self.server = EdgeUnixServer(self.path, build_server_pair())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def _request(self, method: str, path: str, headers=None):
        connection = UnixConnection(self.path)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def test_metadata_is_served_over_the_socket(self) -> None:
        status, body = self._request("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(200, status)
        document = json.loads(body)
        self.assertEqual(ISSUER, document["issuer"])
        # RFC 8414 over a socket is the exact call Caddy makes; if the adapter
        # mishandled the AF_UNIX peer this raises rather than returning.
        self.assertIn("token_endpoint", document)

    def test_the_socket_is_group_readable_and_not_world_accessible(self) -> None:
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(0o660, mode)

    def test_control_routes_are_absent_from_the_unix_edge_listener(self) -> None:
        # /healthz lives only in ControlServer.routes, so its absence here is a
        # property of the object rather than of the Caddy path allowlist.
        status, _ = self._request("GET", "/healthz")
        self.assertEqual(404, status)

    def test_server_close_removes_the_socket_file(self) -> None:
        # Uses its own server on its own path: closing self.server here would
        # leave tearDown calling shutdown() on a stopped loop, which blocks.
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "closing.sock")
            server = EdgeUnixServer(path, build_server_pair())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.assertTrue(os.path.exists(path))
            server.shutdown()
            server.server_close()
            # A leftover socket file would make the next start find its own
            # path occupied and refuse, so removal is part of restartability.
            self.assertFalse(os.path.exists(path))


class StaleSocketTests(unittest.TestCase):
    """P12 allows removing only a *validated* stale socket at the exact path."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "s.sock")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_leftover_socket_with_no_listener_is_removed(self) -> None:
        orphan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        orphan.bind(self.path)
        orphan.close()  # bound then closed: the path survives, nothing listens
        self.assertTrue(os.path.exists(self.path))
        clear_stale_socket(self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_a_live_listener_aborts_startup_and_survives(self) -> None:
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(self.path)
        live.listen(1)
        try:
            with self.assertRaises(SocketPathInUse):
                clear_stale_socket(self.path)
            # The point of refusing: the running server keeps its socket.
            self.assertTrue(os.path.exists(self.path))
        finally:
            live.close()

    def test_a_regular_file_is_never_unlinked(self) -> None:
        Path(self.path).write_text("not a socket")
        with self.assertRaises(SocketPathNotASocket):
            clear_stale_socket(self.path)
        self.assertTrue(os.path.exists(self.path))

    def test_a_missing_path_is_accepted(self) -> None:
        clear_stale_socket(self.path)  # must not raise


class ForwardedForTrustTests(unittest.TestCase):
    """X-Forwarded-For is believed only where the peer cannot be the client.

    Headers are built with the real parser rather than a dict, because the
    defence depends on ``get_all`` -- a dict fake cannot express a repeated
    header, which is exactly the case that used to slip through.
    """

    class _Server:
        def __init__(self, trust):
            self.trust_forwarded_for = trust

    def _handler(self, raw_headers: str, trust: bool):
        message = email.message_from_string(raw_headers)

        class _Handler:
            headers = message
            server = self._Server(trust)
            client_address = ("127.0.0.1", 40000)

        return _Handler()

    def test_a_tcp_listener_ignores_the_forwarding_header(self) -> None:
        # On TCP the header is attacker-supplied: believing it would let one
        # client evade the login throttle by varying a header.
        handler = self._handler("X-Forwarded-For: 203.0.113.9\n", trust=False)
        self.assertEqual("127.0.0.1", client_address(handler))

    def test_a_unix_listener_uses_the_forwarding_header(self) -> None:
        handler = self._handler("X-Forwarded-For: 203.0.113.9\n", trust=True)
        self.assertEqual("203.0.113.9", client_address(handler))

    def test_a_comma_list_is_not_attributed(self) -> None:
        handler = self._handler("X-Forwarded-For: 203.0.113.9, 198.51.100.4\n", trust=True)
        self.assertEqual("unknown", client_address(handler))

    def test_a_repeated_header_is_not_attributed(self) -> None:
        # The other way to write a list. `.get` returns only the first, so this
        # was silently keyed on 3.3.3.3 while the comma form was refused.
        handler = self._handler(
            "X-Forwarded-For: 3.3.3.3\nX-Forwarded-For: 4.4.4.4\n", trust=True
        )
        self.assertEqual("unknown", client_address(handler))

    def test_an_ipv4_mapped_address_folds_to_its_ipv4_form(self) -> None:
        # Otherwise the same client gets two throttle buckets, and so twice
        # the permitted sign-in attempts.
        plain = self._handler("X-Forwarded-For: 9.9.9.9\n", trust=True)
        mapped = self._handler("X-Forwarded-For: ::ffff:9.9.9.9\n", trust=True)
        self.assertEqual(client_address(plain), client_address(mapped))

    def test_a_malformed_address_is_not_attributed(self) -> None:
        handler = self._handler("X-Forwarded-For: not-an-ip\n", trust=True)
        self.assertEqual("unknown", client_address(handler))

    def test_a_missing_header_on_a_unix_listener_is_not_attributed(self) -> None:
        self.assertEqual("unknown", client_address(self._handler("", trust=True)))


class ThrottleTableTests(unittest.TestCase):
    """The throttle table must not grow once per distinct client address."""

    def setUp(self) -> None:
        server._login_attempts.clear()

    def tearDown(self) -> None:
        server._login_attempts.clear()

    def test_expired_windows_are_reclaimed(self) -> None:
        stale = time.monotonic() - (server.LOGIN_WINDOW_SECONDS * 2)
        for index in range(500):
            server._login_attempts[f"198.51.100.{index}"] = [stale]
        self.assertEqual(500, len(server._login_attempts))
        server._throttled("203.0.113.1")
        # Only the live key survives.
        self.assertEqual(["203.0.113.1"], list(server._login_attempts))

    def test_a_live_window_still_throttles(self) -> None:
        for _ in range(server.LOGIN_MAX_ATTEMPTS):
            self.assertFalse(server._throttled("203.0.113.2"))
        self.assertTrue(server._throttled("203.0.113.2"))


if __name__ == "__main__":
    unittest.main()


class DependencySurfaceTests(unittest.TestCase):
    """The component must not acquire a native cryptography stack.

    Every platform image is musl-based and `cryptography` publishes no musl
    wheel, so pulling it in turns a pip install into a source build. The RFCs
    this component uses -- rfc6749, 6750, 7009, 7636, 7662, 8414, 9207 -- need
    none of it, which is why Authlib is installed with --no-deps.

    The assertion is on the Dockerfile rather than on what happens to be
    importable here: this repository's devcontainer has `cryptography`
    installed for other reasons, so a runtime import check would pin the
    developer's machine instead of the image.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def test_authlib_is_installed_without_its_dependencies(self) -> None:
        dockerfile = (self.ROOT / "Dockerfile").read_text(encoding="utf-8")
        nodeps = (self.ROOT / "requirements-nodeps.txt").read_text(encoding="utf-8")
        plain = (self.ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("--no-deps --requirement /app/requirements-nodeps.txt", dockerfile)
        self.assertIn("Authlib==", nodeps)
        # If Authlib ever moves to the ordinary file it would arrive with
        # cryptography, so the split is the control and both halves matter.
        self.assertNotIn("Authlib", plain)

    def test_the_authlib_modules_in_use_import_cleanly(self) -> None:
        """Every RFC module this component relies on, imported for real.

        A stray import into rfc7523/7591/7592/9068/9101 is what would drag the
        native stack in, so the surface is enumerated here rather than assumed.
        """
        for module in (
            "authlib.oauth2.rfc6749",
            "authlib.oauth2.rfc6750",
            "authlib.oauth2.rfc7009",
            "authlib.oauth2.rfc7636",
            "authlib.oauth2.rfc7662",
            "authlib.oauth2.rfc8414",
            "authlib.oauth2.rfc9207",
        ):
            with self.subTest(module=module):
                __import__(module)

    def test_the_component_imports_no_forbidden_authlib_module(self) -> None:
        """Checked against parsed imports, not the file text.

        The forbidden module names appear legitimately in comments explaining
        why they are avoided, so a substring search over the source would flag
        the documentation rather than the dependency.
        """
        import ast

        forbidden = ("rfc7523", "rfc7591", "rfc7592", "rfc9068", "rfc9101")
        for path in sorted(self.ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            for name in imported:
                for banned in forbidden:
                    with self.subTest(module=path.name, imported=name):
                        self.assertNotIn(banned, name)
