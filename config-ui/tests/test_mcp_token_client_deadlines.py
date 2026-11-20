"""Config sender limits, measured across the complete OAuth POST."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import selectors
import socket
import threading
import time
import unittest
from unittest.mock import patch

import mcp_token_client as client
from control_connections import AdmissionTimeout, request_scope


class TokenClientDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.budgets = []
        self.reply = self.healthy
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                fixture.budgets.append(float(self.headers["X-Mapp-Timeout-Seconds"]))
                with suppress(BrokenPipeError, ConnectionResetError):
                    fixture.reply(self)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        self.tokens = client.McpTokenClient(f"http://127.0.0.1:{server.server_port}",
                                            "config", "secret", resource="config", timeout=2)

    @staticmethod
    def healthy(handler):
        handler.wfile.write(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n'
                            b'Content-Length: 17\r\n\r\n{"redeemed":true}')

    def redeem(self):
        return self.tokens.redeem("token", "xyz.reload", "digest")

    def test_trickling_headers_and_body_obey_both_parent_and_hop_deadlines(self):
        for headers in (False, True):
            for parent_limit, hop_limit in ((.15, 2), (2, .15)):
                with self.subTest(headers=headers, parent=parent_limit):
                    handlers = queue.Queue()

                    def trickle(handler):
                        handlers.put(threading.current_thread())
                        handler.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n")
                        if headers:
                            handler.wfile.write(b"X-Trickle: ")
                        else:
                            handler.wfile.write(b"Content-Length: 64\r\n\r\n")
                        for _ in range(60):
                            handler.wfile.write(b" ")
                            time.sleep(.01)

                    self.reply = trickle
                    self.tokens.timeout = hop_limit
                    start = time.monotonic()
                    with request_scope(parent_limit), self.assertRaises(client.RedemptionUnconfirmed):
                        self.redeem()
                    elapsed = time.monotonic() - start
                    self.assertGreaterEqual(elapsed, .14)
                    self.assertLess(elapsed, .25)  # 100 ms scheduler tolerance
                    handler = handlers.get(timeout=1)
                    handler.join(1)
                    self.assertFalse(handler.is_alive())
                    self.reply = self.healthy
                    self.assertTrue(self.redeem()["redeemed"])

    def test_unusable_acknowledgements_are_indeterminate_and_never_replayed(self):
        for status, framing, body in (
            (b"200 OK", b"Content-Length: 24\r\n", b'{"redeemed":true}'),
            (b"200 OK", b"", b'{"redeemed":true}'),
            (b"200 OK", b"Content-Length: 2\r\n", b"{}"),
            (b"503 Busy", b"Content-Length: 2\r\n", b"{}"),
            (b"307 Redirect", b"Location: /unexpected\r\nContent-Length: 2\r\n", b"{}"),
            (b"200 OK", b"Content-Length: 65537\r\n", b'{"redeemed":true}' + b" " * (65537 - 17)),
        ):
            with self.subTest(status=status, framing=framing):
                def reply(handler):
                    handler.wfile.write(b"HTTP/1.0 " + status + b"\r\nContent-Type: application/json\r\n"
                                        + framing + b"\r\n" + body)

                self.reply = reply
                before = len(self.budgets)
                with self.assertRaises(client.RedemptionUnconfirmed):
                    self.redeem()
                self.assertEqual(before + 1, len(self.budgets))
        self.reply = self.healthy
        self.assertTrue(self.redeem()["redeemed"])

    def test_binding_refusal_preserves_status_and_scrubs_credentials(self):
        def refuse(handler):
            detail = "secret token " + handler.headers["Authorization"]
            body = json.dumps({"error": "invalid_grant", "error_description": detail}).encode()
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        self.reply = refuse
        with self.assertRaises(client.McpTokenClientError) as caught:
            self.redeem()
        self.assertTrue(caught.exception.refused)
        self.assertEqual((400, "invalid_grant"), (caught.exception.status, caught.exception.error))
        self.assertEqual("[redacted] [redacted] Basic [redacted]", str(caught.exception))

    def test_resolution_time_is_deducted_at_send_across_two_calls(self):
        lookup = socket.getaddrinfo

        def delayed(*args, **kwargs):
            time.sleep(.08)
            return lookup(*args, **kwargs)

        with patch.object(socket, "getaddrinfo", delayed), request_scope(.8):
            self.redeem()
            self.redeem()
        first, second = self.budgets
        self.assertLess(first, .73)
        self.assertLess(second, first - .07)

    def test_parent_cancellation_during_connect_prevents_submission(self):
        pending = threading.Event()
        requests = queue.Queue()

        class ObserveConnect(selectors.DefaultSelector):
            def select(self, timeout=None):
                ready = super().select(timeout)
                if not ready:
                    pending.set()
                return ready

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(0)
            address = listener.getsockname()
            blocked = client.McpTokenClient(f"http://127.0.0.1:{address[1]}", "config", "secret",
                                            resource="config", timeout=2)

            def caller():
                with request_scope(2) as request:
                    requests.put(request)
                    blocked.redeem("token", "xyz.reload", "digest")

            # Fill the Linux accept queue, then observe the second connection
            # actually waiting for writability before cancelling its request.
            with socket.create_connection(address, timeout=1), ThreadPoolExecutor(1) as worker:
                with patch.object(selectors, "DefaultSelector", ObserveConnect):
                    future = worker.submit(caller)
                    self.assertTrue(pending.wait(1))
                    requests.get(timeout=1).cancel("fixture")
                    with self.assertRaises(AdmissionTimeout):
                        future.result(.25)
        self.assertTrue(self.redeem()["redeemed"])

    def test_cancelled_dns_retains_each_slot_and_cannot_connect_late(self):
        lookup = socket.getaddrinfo
        entered = queue.Queue()
        release = threading.Event()
        requests = queue.Queue()
        reclaimed = queue.Queue()
        release_slot = client._DNS_SLOTS.release

        def observe_reclamation():
            release_slot()
            reclaimed.put(True)

        def blocked(*args, **kwargs):
            entered.put(True)
            if not release.wait(4):
                raise RuntimeError("resolver fixture timed out")
            return lookup(*args, **kwargs)

        def caller():
            with request_scope(2) as request:
                requests.put(request)
                self.redeem()

        with (patch.object(socket, "getaddrinfo", blocked),
              patch.object(client._DNS_SLOTS, "release", observe_reclamation),
              ThreadPoolExecutor(client._DNS_LIMIT) as workers):
            futures = [workers.submit(caller) for _ in range(client._DNS_LIMIT)]
            try:
                for _ in futures:
                    entered.get(timeout=1)
                for _ in futures:
                    requests.get(timeout=1).cancel("fixture")
                for future in futures:
                    with self.assertRaises(AdmissionTimeout):
                        future.result(1)
                with self.assertRaisesRegex(client.McpTokenClientError, "at capacity"):
                    self.redeem()
            finally:
                release.set()
                for _ in futures:
                    reclaimed.get(timeout=1)
        self.assertTrue(self.redeem()["redeemed"])
        self.assertEqual(1, len(self.budgets))
