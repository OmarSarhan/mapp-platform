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


def semantic_program_without_retry_waits() -> str:
    program = semantic_program()
    marker = "time.sleep(delay)"
    if program.count(marker) != 1:
        raise AssertionError("demo retry sleep marker changed")
    return program.replace(marker, "None")


class SemanticApi(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_arguments) -> None:
        pass

    def _reply(self, payload: dict, status: int = 200) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
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
                    "curated": server.curated,
                },
            })
            return
        if self.path == "/api/semantic/generate":
            target = body["target"]
            name = (
                "table" if target["kind"] == "table"
                else target["fieldId"]
            )
            time.sleep(server.delays.get(name, 0))
            with server.records_lock:
                server.completed_targets.append(name)
                server.generated_targets.append(target)
                attempt = server.target_attempts.get(name, 0) + 1
                server.target_attempts[name] = attempt
            if attempt <= server.rate_limit_failures.get(name, 0):
                self._reply(
                    {"code": server.rate_limit_codes.get(
                        name,
                        "semantic.generation_rate_limited",
                    )},
                    429,
                )
            elif name == server.failed_target:
                self._reply({"code": "semantic.generation_context_unavailable"})
            elif name in server.no_change:
                self._reply({"code": "semantic.generation_no_change"})
            else:
                self._reply({
                    "draft": {
                        "baseVersion": server.asset_version,
                        "operations": [
                            {"op": "set", "target": name, "index": index}
                            for index in range(server.operations_per_target)
                        ],
                    },
                })
            return
        if self.path == "/api/semantic/proposals/check":
            if len(body["operations"]) > server.max_operations:
                self._reply({"code": "semantic.invalid_request"})
                return
            if body["baseVersion"] != server.asset_version:
                self._reply({"code": "semantic.revision_conflict"})
                return
            with server.records_lock:
                server.checked_operations = body["operations"]
                server.checked_batches.append(body["operations"])
                server.checked_base_versions.append(body["baseVersion"])
            self._reply({"check": {"fingerprint": "checked"}})
            return
        if self.path == "/api/semantic/proposals":
            self._reply({"proposal": {"id": "proposal-1"}})
            return
        if self.path == "/api/semantic/proposals/proposal-1/apply":
            server.asset_version += 1
            self._reply({
                "proposal": {"state": "applied"},
                "asset": {"version": server.asset_version},
            })
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
        operations_per_target: int = 1,
        max_operations: int = 100,
        curated: dict | None = None,
        rate_limit_failures: dict[str, int] | None = None,
        rate_limit_codes: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess, ThreadingHTTPServer]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), SemanticApi)
        server.daemon_threads = True
        server.fields = fields
        server.delays = delays or {}
        server.no_change = no_change or set()
        server.failed_target = failed_target
        server.curated = curated or {}
        server.rate_limit_failures = rate_limit_failures or {}
        server.rate_limit_codes = rate_limit_codes or {}
        server.target_attempts = {}
        server.asset_version = 7
        server.operations_per_target = operations_per_target
        server.max_operations = max_operations
        server.generated_targets = []
        server.completed_targets = []
        server.checked_operations = None
        server.checked_batches = []
        server.checked_base_versions = []
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
        program = semantic_program_without_retry_waits()
        try:
            if not tty_output:
                result = subprocess.run(
                    command,
                    input=program,
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
                        program,
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
            r"Gemini descriptions \[[#-]+\] (\d+/\d+) targets settled",
            output,
        )

    def test_progress_tracks_completed_targets_and_preserves_order(self) -> None:
        result, server = self.run_semantics(
            ["field-1", "field-2"],
            limit="50",
            delays={"table": 0.06, "field-1": 0.03, "field-2": 0.001},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            "MAPP_DEMO_FIELD_LIMIT=50; the first 50 fields per relation are included",
            result.stdout,
        )
        self.assertEqual(
            ["0/3", "1/3", "2/3", "3/3"],
            self.progress_counts(result.stdout),
        )
        self.assertEqual(
            ["table", "field-2", "field-1"],
            server.completed_targets,
        )
        self.assertEqual(
            ["table", "field-1", "field-2"],
            [operation["target"] for operation in server.checked_operations],
        )
        self.assertLess(
            result.stdout.rindex("3/3 targets settled"),
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
            r"\r    Gemini descriptions \[[#-]+\] 1/1 targets settled \(100%\)\n"
            r"    source\.relation: table and 0 fields described\n$",
        )

    def test_limit_counts_the_table_and_first_fields_for_a_wide_relation(self) -> None:
        result, server = self.run_semantics(
            ["field-1", "field-2"],
            limit="1",
            operations_per_target=60,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["0/2", "1/2", "2/2"],
            self.progress_counts(result.stdout),
        )
        self.assertCountEqual(
            [
                {"kind": "table"},
                {"kind": "field", "fieldId": "field-1"},
            ],
            server.generated_targets,
        )
        self.assertIn(
            "table and first 1 of 2 fields described, 1 remaining field "
            "not described because MAPP_DEMO_FIELD_LIMIT=1",
            result.stdout,
        )
        self.assertEqual([100, 20], [len(batch) for batch in server.checked_batches])
        self.assertEqual([7, 8], server.checked_base_versions)

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

    def test_repeat_skips_every_already_curated_target(self) -> None:
        result, server = self.run_semantics(
            ["field-1", "field-2"],
            limit="50",
            curated={
                "description": "Existing table description",
                "fields": {
                    "field-1": {"description": "Existing field one"},
                    "field-2": {"tags": ["existing"]},
                },
            },
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], server.generated_targets)
        self.assertEqual(["0/0"], self.progress_counts(result.stdout))
        self.assertIn("source.relation: already described", result.stdout)
        self.assertIsNone(server.checked_operations)

    def test_repeat_generates_only_targets_without_curated_annotations(self) -> None:
        result, server = self.run_semantics(
            ["field-1", "field-2"],
            limit="50",
            curated={
                "description": "Existing table description",
                "fields": {
                    "field-1": {"description": "Existing field one"},
                },
            },
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [{"kind": "field", "fieldId": "field-2"}],
            server.generated_targets,
        )
        self.assertEqual(["0/1", "1/1"], self.progress_counts(result.stdout))
        self.assertEqual("field-2", server.checked_operations[0]["target"])

    def test_transient_rate_limit_waits_and_retries_the_same_target(self) -> None:
        result, server = self.run_semantics(
            ["field-1"],
            limit="50",
            rate_limit_failures={"table": 2},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(3, server.target_attempts["table"])
        self.assertEqual(1, server.target_attempts["field-1"])
        self.assertIn("waiting 5 seconds before retry 1/3", result.stdout)
        self.assertIn("waiting 15 seconds before retry 2/3", result.stdout)
        self.assertNotIn("retry 3/3", result.stdout)
        self.assertNotIn("Gemini descriptions paused", result.stderr)
        self.assertEqual(
            ["table", "field-1"],
            [operation["target"] for operation in server.checked_operations],
        )

    def test_exhausted_rate_limit_retries_then_keeps_demo_successful(self) -> None:
        result, server = self.run_semantics(
            ["field-%d" % index for index in range(20)],
            limit="50",
            rate_limit_failures={"table": 4},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(4, server.target_attempts["table"])
        self.assertEqual(
            [{"kind": "table"}] * 4,
            server.generated_targets,
        )
        self.assertIn("waiting 5 seconds before retry 1/3", result.stdout)
        self.assertIn("waiting 15 seconds before retry 2/3", result.stdout)
        self.assertIn("waiting 45 seconds before retry 3/3", result.stdout)
        self.assertIn("stopped at 0/21 targets settled", result.stdout)
        self.assertIn("Gemini descriptions paused", result.stderr)
        self.assertIn("remained rate limited after 3 retries", result.stderr)
        self.assertIn("Google AI Studio", result.stderr)
        self.assertIn("rerun ./bin/mapp demo", result.stderr)
        self.assertIsNone(server.checked_operations)

    def test_exhausted_local_capacity_has_local_retry_guidance(self) -> None:
        result, server = self.run_semantics(
            [],
            limit="50",
            rate_limit_failures={"table": 4},
            rate_limit_codes={"table": "semantic.generation_busy"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(4, server.target_attempts["table"])
        self.assertIn(
            "configuration service remained at Gemini generation capacity",
            result.stderr,
        )
        self.assertIn("Let other generation requests finish", result.stderr)
        self.assertNotIn("Google AI Studio", result.stderr)
        self.assertIsNone(server.checked_operations)

    def test_mid_relation_rate_limit_uses_one_probe_and_no_later_window(self) -> None:
        fields = ["field-%d" % index for index in range(20)]
        result, server = self.run_semantics(
            fields,
            limit="50",
            rate_limit_failures={name: 4 for name in fields[:8]},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(4, server.target_attempts["field-0"])
        self.assertTrue(all(
            server.target_attempts[name] == 1 for name in fields[1:8]
        ))
        self.assertNotIn("field-8", server.completed_targets)
        self.assertEqual(12, len(server.completed_targets))
        self.assertIn("stopped at 1/21 targets settled", result.stdout)
        self.assertIn("Gemini descriptions paused", result.stderr)
        self.assertIsNone(server.checked_operations)

    def test_persistent_mixed_batch_discards_unfinished_relation_drafts(self) -> None:
        fields = ["field-%d" % index for index in range(20)]
        result, server = self.run_semantics(
            fields,
            limit="50",
            rate_limit_failures={"field-0": 4},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(4, server.target_attempts["field-0"])
        self.assertTrue(all(
            server.target_attempts[name] == 1 for name in fields[1:8]
        ))
        self.assertNotIn("field-8", server.target_attempts)
        self.assertIn("stopped at 8/21 targets settled", result.stdout)
        self.assertIn(
            "completed target results from the unfinished relation were not "
            "applied",
            result.stderr,
        )
        self.assertIsNone(server.checked_operations)

    def test_mid_relation_recovery_keeps_successes_and_finishes_serially(self) -> None:
        fields = ["field-%d" % index for index in range(10)]
        result, server = self.run_semantics(
            fields,
            limit="50",
            delays={"field-0": 0.05},
            rate_limit_failures={"field-0": 1},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, server.target_attempts["field-0"])
        self.assertTrue(all(
            server.target_attempts[name] == 1 for name in fields[1:]
        ))
        self.assertIn("waiting 5 seconds before retry 1/3", result.stdout)
        self.assertNotIn("Gemini descriptions paused", result.stderr)
        self.assertEqual(
            ["table", *fields],
            [operation["target"] for operation in server.checked_operations],
        )

    def test_non_rate_limit_batch_failure_is_not_hidden_by_a_429(self) -> None:
        result, server = self.run_semantics(
            ["field-0", "field-1"],
            limit="50",
            failed_target="field-1",
            rate_limit_failures={"field-0": 1},
        )

        self.assertEqual(1, result.returncode)
        self.assertNotIn("waiting", result.stdout)
        self.assertNotIn("Gemini descriptions paused", result.stderr)
        self.assertIn("generate field", result.stderr)
        self.assertEqual(1, server.target_attempts["field-0"])
        self.assertIsNone(server.checked_operations)

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
        self.assertEqual([{"kind": "table"}], server.generated_targets)
        self.assertIsNone(server.checked_operations)


if __name__ == "__main__":
    unittest.main()
