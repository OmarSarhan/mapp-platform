import fcntl
import socket
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from control_connections import AdmissionTimeout, current_request, remaining_time, request_scope


class RequestBudgetTests(unittest.TestCase):
    def test_high_connected_fd_is_admitted_and_input_is_preserved(self):
        left, right = socket.socketpair()
        with left, right, socket.socket(fileno=fcntl.fcntl(left, fcntl.F_DUPFD_CLOEXEC, 1100)) as high:
            with request_scope(2, high):
                self.assertGreaterEqual(high.fileno(), 1100)
                self.assertGreater(remaining_time(), 0)
                right.sendall(b"x")
                self.assertGreater(remaining_time(), 0)
                self.assertEqual(high.recv(1), b"x")

    def test_inconclusive_probe_does_not_latch_and_eof_still_does(self):
        class Peer(socket.socket):
            fail_probe = True

            def fileno(self):
                if self.fail_probe:
                    self.fail_probe = False
                    raise OSError("transient observation failure")
                return super().fileno()

        left, right = socket.socketpair()
        with Peer(fileno=left.detach()) as peer, right, request_scope(2, peer):
            self.assertGreater(remaining_time(), 0)
            self.assertFalse(peer.fail_probe)
            right.close()
            with self.assertRaisesRegex(AdmissionTimeout, "disconnected"):
                remaining_time()

    def test_finish_joins_late_registration_already_aborting(self):
        entered, release, finishing = (threading.Event() for _ in range(3))
        with request_scope(5) as request, ThreadPoolExecutor(2) as executor:
            request.cancel("fixture")

            def abort():
                entered.set()
                self.assertTrue(release.wait(2))

            def finish():
                finishing.set()
                request.finish()

            registered = executor.submit(request.arm, abort)
            self.assertTrue(entered.wait(1))
            joined = executor.submit(finish)
            try:
                self.assertTrue(finishing.wait(1))
                with self.assertRaises(TimeoutError):
                    joined.result(.05)
            finally:
                release.set()
            joined.result(1)
            with self.assertRaises(AdmissionTimeout):
                registered.result(1)

    def test_disarm_joins_and_one_blocked_abort_cannot_delay_a_sibling(self):
        entered, finish, sibling, detached = (threading.Event() for _ in range(4))

        def slow_abort():
            entered.set()
            finish.wait(2)

        with request_scope(10) as request, ThreadPoolExecutor(1) as executor:
            self.assertIs(request, current_request())
            self.assertLessEqual(remaining_time(.05), .05)
            request.arm(detached.set)()
            disarm = request.arm(slow_abort)
            other_disarm = request.arm(sibling.set)
            request.cancel("fixture cancelled")
            try:
                self.assertTrue(entered.wait(1))
                joined = executor.submit(disarm)
                with self.assertRaises(TimeoutError):
                    joined.result(timeout=.05)
                self.assertTrue(sibling.wait(.5))
                with self.assertRaises(AdmissionTimeout):
                    remaining_time()
                late = threading.Event()
                with self.assertRaises(AdmissionTimeout):
                    request.arm(late.set)
                self.assertTrue(late.is_set())
            finally:
                finish.set()
            joined.result(timeout=1)
            other_disarm()
        self.assertFalse(detached.is_set())
        self.assertIsNone(current_request())

    def test_registered_resource_observes_deadline_or_idle_peer_eof(self):
        for peer_eof in (False, True):
            left, right = socket.socketpair()
            with self.subTest(peer_eof=peer_eof), left, right:
                with request_scope(2 if peer_eof else .05, left) as request:
                    aborted = threading.Event()
                    disarm = request.arm(aborted.set)
                    if peer_eof:
                        right.close()
                    self.assertTrue(aborted.wait(.5))
                    disarm()
