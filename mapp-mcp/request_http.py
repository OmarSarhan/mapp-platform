"""Keep urllib's handlers and TLS policy; own the socket through the last byte."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from functools import partial
import http.client
from io import BytesIO
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from request_scope import CURRENT_REQUEST


# getaddrinfo cannot be interrupted. Refuse saturation before submitting to
# the executor, and count the job until the resolver itself exits.
_RESOLVER_LIMIT = 8
_RESOLVERS = ThreadPoolExecutor(_RESOLVER_LIMIT, thread_name_prefix="mapp-dns")
_RESOLVER_SLOTS = threading.BoundedSemaphore(_RESOLVER_LIMIT)


class _Hop:
    def __init__(self, request, timeout):
        self.request = request
        self.deadline = time.monotonic() + request.remaining(timeout)
        self.lock = threading.Lock()
        self.handle = None

    def remaining(self):
        return self.request.remaining(self.deadline - time.monotonic())

    def abort(self):
        # The duplicate stays valid when TLS or HTTPResponse takes the original
        # fd. Serialize shutdown and disarm so it never reaches a later socket.
        with self.lock:
            if self.handle is not None:
                with suppress(OSError):
                    self.handle.shutdown(socket.SHUT_RDWR)

    def disarm(self):
        with self.lock:
            if self.handle is not None:
                self.handle.close()
                self.handle = None

    def create_socket(self, address, timeout, source_address):
        self.disarm()  # urllib may open another connection on a redirect
        future = asyncio.run_coroutine_threadsafe(self.connect(address), self.request.loop)
        return future.result()

    async def connect(self, address):
        loop = self.request.loop
        with self.request.active(self.remaining()):
            if not _RESOLVER_SLOTS.acquire(blocking=False):
                raise OSError("Downstream name resolution is at capacity.")
            try:
                lookup = _RESOLVERS.submit(socket.getaddrinfo, *address, type=socket.SOCK_STREAM)
            except BaseException:
                _RESOLVER_SLOTS.release()
                raise
            lookup.add_done_callback(lambda _: _RESOLVER_SLOTS.release())
            # Cancelling the HTTP waiter must never cancel a queued executor
            # future and return its slot while its work item is still queued.
            resolved = asyncio.wrap_future(lookup)
            resolved.add_done_callback(lambda done: done.exception())
            await asyncio.wait([resolved])
            addresses = resolved.result()
            last_error = OSError("No address resolved for the downstream service.")
            for family, kind, proto, _, sockaddr in addresses:
                self.remaining()
                sock = socket.socket(family, kind, proto)
                try:
                    sock.setblocking(False)
                    await loop.sock_connect(sock, sockaddr)
                    sock.settimeout(self.remaining())
                    with self.lock:
                        self.remaining()
                        self.handle = sock.dup()
                    return sock
                except BaseException as exc:
                    sock.close()
                    if not isinstance(exc, OSError):
                        raise
                    last_error = exc
            raise last_error


class _Connection:
    def __init__(self, host, *, hop, **options):
        super().__init__(host, **options)
        self.hop = hop
        self._create_connection = hop.create_socket

    def request(self, method, url, body, headers, **options):
        self.connect()
        headers["X-Mapp-Timeout-Seconds"] = str(self.hop.remaining())
        super().request(method, url, body, headers, **options)


class _HTTP(_Connection, http.client.HTTPConnection):
    pass


class _HTTPS(_Connection, http.client.HTTPSConnection):
    pass


class _OwnedHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, hop):
        super().__init__()
        self.hop = hop

    def do_open(self, connection_type, req, **options):
        cls = _HTTPS if connection_type is http.client.HTTPSConnection else _HTTP
        return super().do_open(partial(cls, hop=self.hop), req, **options)


class _HTTPRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in {"http", "https"}:
            raise urllib.error.HTTPError(req.full_url, code, "Unsupported redirect scheme", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def framed_length(headers):
    """Only an unambiguous byte count proves completion across local abort."""
    lengths = headers.get_all("Content-Length", [])
    if headers.get_all("Transfer-Encoding", []) or len(lengths) != 1:
        return None
    length = lengths[0].strip(" \t")
    return int(length) if length.isascii() and length.isdigit() else None


@contextmanager
def urlopen(req, *, timeout, read_error_body=False):
    request = CURRENT_REQUEST.get()
    if request is None:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            yield response
        return

    hop = _Hop(request, timeout)
    timer = threading.Timer(hop.remaining(), hop.abort)
    with request.own_hop(hop):
        timer.start()
        try:
            opener = urllib.request.build_opener(_OwnedHandler(hop), _HTTPRedirects())
            try:
                response = opener.open(req, timeout=hop.remaining())
            except urllib.error.HTTPError as exc:
                # Clients decode the refusal outside this context. Drain it
                # while cancellation still owns the wire.
                with exc:
                    detail = exc.read() if read_error_body else b""
                if not read_error_body or framed_length(exc.headers) != len(detail):
                    hop.remaining()
                raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.headers, BytesIO(detail)) from None
            with response:
                yield response
                hop.remaining()
        except urllib.error.HTTPError:
            # The authoritative response above already completed its wire check.
            raise
        except OSError:
            hop.remaining()
            raise
        except http.client.HTTPException:
            hop.remaining()
            raise OSError("The downstream HTTP response was incomplete.") from None
        finally:
            timer.cancel()
            hop.disarm()
