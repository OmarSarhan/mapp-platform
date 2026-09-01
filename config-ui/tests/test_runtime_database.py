import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from runtime_database import (
    MAX_CONCURRENT_DBS_CONNECTIONS,
    dbs_connection,
)


class RuntimeDatabaseAdmissionTests(unittest.TestCase):
    def test_connect_callable_never_observes_more_than_eight_sessions(self) -> None:
        active = 0
        maximum = 0
        calls = 0
        lock = threading.Lock()
        admitted = threading.Event()
        release = threading.Event()

        class Connection:
            def __enter__(self):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active == MAX_CONCURRENT_DBS_CONNECTIONS:
                        admitted.set()
                return self

            def __exit__(self, *_args):
                nonlocal active
                with lock:
                    active -= 1

        def connect(_url, **_kwargs):
            nonlocal calls
            with lock:
                calls += 1
            return Connection()

        def worker() -> None:
            with dbs_connection(connect, "postgresql://runtime"):
                self.assertTrue(release.wait(2))

        workers = MAX_CONCURRENT_DBS_CONNECTIONS + 1
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker) for _ in range(workers)]
            try:
                self.assertTrue(admitted.wait(2))
                # Give the ninth worker a chance to reach the semaphore.  It
                # must not call the supplied connector until a permit returns.
                time.sleep(0.05)
                with lock:
                    self.assertEqual(MAX_CONCURRENT_DBS_CONNECTIONS, calls)
                    self.assertEqual(MAX_CONCURRENT_DBS_CONNECTIONS, maximum)
            finally:
                release.set()
            for future in futures:
                future.result(timeout=2)

        self.assertEqual(0, active)
        self.assertEqual(workers, calls)

    def test_failed_connect_releases_its_permit(self) -> None:
        attempts = 0

        def connect(_url):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("connect failed")

            class Connection:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

            return Connection()

        with self.assertRaisesRegex(RuntimeError, "connect failed"):
            with dbs_connection(connect, "postgresql://runtime"):
                pass
        with dbs_connection(connect, "postgresql://runtime"):
            pass
        self.assertEqual(2, attempts)

    def test_nested_connection_fails_instead_of_waiting_on_its_own_slot(self) -> None:
        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        def connect(_url):
            return Connection()

        with dbs_connection(connect, "postgresql://outer"):
            with self.assertRaisesRegex(RuntimeError, "Nested DBS_\\*"):
                with dbs_connection(connect, "postgresql://inner"):
                    pass

        # The rejected nested attempt must not poison later sequential work.
        with dbs_connection(connect, "postgresql://later"):
            pass


if __name__ == "__main__":
    unittest.main()
