from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import struct
import subprocess
import sys
import termios
import threading
import time
import tty as tty_module
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def semantic_program() -> str:
    source = (ROOT / "docker/demo-sources/layers.sh").read_text(encoding="utf-8")
    function = source.index("describe_relations()")
    start = source.index("<<'PY'\n", function) + len("<<'PY'\n")
    end = source.index("\nPY\n", start)
    return source[start:end]


class SemanticApi(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_arguments) -> None:
        pass

    def _reply(self, payload: dict) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        server = self.server
        if self.path == "/api/semantic/source/sync":
            self._reply({
                "asset": {
                    "id": "asset-1",
                    "generated": {
                        "fields": [{"id": field} for field in server.fields],
                    },
                },
            })
            return
        if self.path == "/api/semantic/generate":
            target = body["target"]
            name = (
                "table" if target["kind"] == "table"
                else target["fieldId"]
            )
            if server.generate_barrier is not None:
                server.generate_barrier.wait(timeout=2)
            time.sleep(server.delays.get(name, 0))
            with server.records_lock:
                server.completed_targets.append(name)
                server.generated_targets.append(target)
            if name == server.failed_target:
                self._reply({"code": "semantic.generation_context_unavailable"})
            elif name in server.no_change:
                self._reply({"code": "semantic.generation_no_change"})
            else:
                self._reply({
                    "draft": {
                        "baseVersion": 7,
                        "operations": [{"op": "set", "target": name}],
                    },
                })
            return
        if self.path == "/api/semantic/proposals/check":
            with server.records_lock:
                server.checked_operations = body["operations"]
            self._reply({"check": {"fingerprint": "checked"}})
            return
        if self.path == "/api/semantic/proposals":
            self._reply({"proposal": {"id": "proposal-1"}})
            return
        if self.path == "/api/semantic/proposals/proposal-1/apply":
            self._reply({"proposal": {"state": "applied"}})
            return
        self.send_error(404)


class DemoSemanticProgressTests(unittest.TestCase):
    def run_semantics(
        self,
        fields: list[str],
        *,
        limit: str,
        delays: dict[str, float] | None = None,
        no_change: set[str] | None = None,
        failed_target: str | None = None,
        tty_output: bool = False,
        synchronized_calls: int | None = None,
    ) -> tuple[subprocess.CompletedProcess, ThreadingHTTPServer]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), SemanticApi)
        server.daemon_threads = True
        server.fields = fields
        server.delays = delays or {}
        server.no_change = no_change or set()
        server.failed_target = failed_target
        server.generate_barrier = (
            threading.Barrier(synchronized_calls)
            if synchronized_calls is not None else None
        )
        server.generated_targets = []
        server.completed_targets = []
        server.checked_operations = None
        server.records_lock = threading.Lock()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = os.environ.copy()
        for name in (
            "ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
            "all_proxy", "https_proxy", "http_proxy",
        ):
            environment.pop(name, None)
        environment.update({
            "NO_PROXY": "127.0.0.1,localhost",
            "MAPP_BASE": "http://127.0.0.1:%d" % server.server_port,
            "MAPP_HOST": "config.localhost",
            "MAPP_TOKEN": "test-token",
            "MAPP_FIELD_LIMIT": limit,
        })
        command = [sys.executable, "-", "source", "relation"]
        try:
            if not tty_output:
                result = subprocess.run(
                    command,
                    input=semantic_program(),
                    capture_output=True,
                    text=True,
                    env=environment,
                    check=False,
                    timeout=10,
                )
            else:
                master, slave = pty.openpty()
                tty_module.setraw(slave)
                fcntl.ioctl(
                    slave,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 100, 0, 0),
                )
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=slave,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                os.close(slave)
                try:
                    _, stderr = process.communicate(
                        semantic_program(),
                        timeout=10,
                    )
                    chunks = []
                    while True:
                        try:
                            chunk = os.read(master, 4096)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                break
                            raise
                        if not chunk:
                            break
                        chunks.append(chunk)
                finally:
                    os.close(master)
                result = subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    b"".join(chunks).decode(),
                    stderr,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        return result, server

    @staticmethod
    def progress_counts(output: str) -> list[str]:
        return re.findall(
            r"Gemini descriptions \[[#-]+\] (\d+/\d+) calls",
            output,
        )

    def test_progress_tracks_settled_calls_and_preserves_target_order(self) -> None:
        result, server = self.run_semantics(
            ["field-1", "field-2"],
            limit="50",
            delays={"table": 0.06, "field-1": 0.03, "field-2": 0.001},
            synchronized_calls=3,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            "MAPP_DEMO_FIELD_LIMIT=50; relations above 50 fields are table-only",
            result.stdout,
        )
        self.assertEqual(
            ["0/3", "1/3", "2/3", "3/3"],
            self.progress_counts(result.stdout),
        )
        self.assertEqual(
            ["field-2", "field-1", "table"],
            server.completed_targets,
        )
        self.assertEqual(
            ["table", "field-1", "field-2"],
            [operation["target"] for operation in server.checked_operations],
        )
        self.assertLess(
            result.stdout.rindex("3/3 calls"),
            result.stdout.index("source.relation: table and 2 fields described"),
        )
        self.assertNotIn("\r", result.stdout)
        self.assertTrue(result.stdout.endswith("\n"))

    def test_no_change_still_advances_the_progress_bar(self) -> None:
        result, server = self.run_semantics(
            ["field-1"],
            limit="50",
            no_change={"table", "field-1"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["0/2", "1/2", "2/2"],
            self.progress_counts(result.stdout),
        )
        self.assertIn("source.relation: already described", result.stdout)
        self.assertIsNone(server.checked_operations)

    def test_tty_uses_one_in_place_bar_and_ends_it_before_the_summary(self) -> None:
        result, _server = self.run_semantics(
            [],
            limit="50",
            tty_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("\r    Gemini descriptions [", result.stdout)
        self.assertNotIn("\x1b", result.stdout)
        self.assertRegex(
            result.stdout,
            r"\r    Gemini descriptions \[[#-]+\] 1/1 calls \(100%\)\n"
            r"    source\.relation: table and 0 fields described\n$",
        )

    def test_limit_counts_only_the_table_for_a_wide_relation(self) -> None:
        result, server = self.run_semantics(
            ["field-1", "field-2"],
            limit="1",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["0/1", "1/1"], self.progress_counts(result.stdout))
        self.assertEqual([{"kind": "table"}], server.generated_targets)
        self.assertIn(
            "2 fields over the MAPP_DEMO_FIELD_LIMIT of 1 not described",
            result.stdout,
        )

    def test_empty_limit_counts_every_field(self) -> None:
        result, server = self.run_semantics(
            ["field-1", "field-2"],
            limit="",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            "MAPP_DEMO_FIELD_LIMIT is empty; all fields are included",
            result.stdout,
        )
        self.assertEqual(
            ["0/3", "1/3", "2/3", "3/3"],
            self.progress_counts(result.stdout),
        )
        self.assertEqual(3, len(server.generated_targets))

    def test_failure_ends_the_bar_without_applying_partial_drafts(self) -> None:
        result, server = self.run_semantics(
            ["field-1"],
            limit="50",
            failed_target="table",
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("Gemini descriptions stopped at", result.stdout)
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertIn("generate table", result.stderr)
        self.assertIsNone(server.checked_operations)


if __name__ == "__main__":
    unittest.main()
