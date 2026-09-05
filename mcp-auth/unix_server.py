"""AF_UNIX listeners for the authorization component.

`http.server` assumes an AF_INET address throughout, so three methods need
overriding. Each override below exists because a specific stdlib line fails on a
path string, not for symmetry:

* ``HTTPServer.server_bind`` runs ``host, port = self.server_address[:2]``,
  which slices a path *string* into its first two characters and then hands
  those to ``socket.getfqdn``.
* ``TCPServer.get_request`` returns the accepted peer address, which for
  AF_UNIX is ``''``. ``BaseHTTPRequestHandler.address_string`` then evaluates
  ``self.client_address[0]`` and raises ``IndexError`` on the empty string.
* ``TCPServer.server_close`` closes the listening socket but leaves the path on
  disk, so the next start would find its own socket file and treat it as stale.
"""

from __future__ import annotations

import os
import socket
import stat

#: Reported as the peer for every Unix-socket connection. The socket is
#: exclusive to Caddy (P12), so the real client address arrives in
#: X-Forwarded-For, which Caddy overwrites rather than appends.
UNIX_PEER = "unix"


class SocketPathInUse(RuntimeError):
    """The configured path is already served by a live process."""


class SocketPathNotASocket(RuntimeError):
    """The configured path exists and is not a socket."""


def clear_stale_socket(path: str) -> None:
    """Remove ``path`` only if it is a socket with no listener behind it.

    P12 permits removing "only a validated stale socket at that exact
    configured path". Validation is what makes this safe: a live listener must
    abort startup rather than be silently displaced, and a non-socket must
    never be unlinked, so that a misconfigured path cannot delete a real file.
    """
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise SocketPathNotASocket(path)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        # Nothing is listening: the file is a leftover from an unclean stop.
        os.unlink(path)
        return
    except OSError:
        # Any other error leaves the path alone. Refusing to start is the safe
        # outcome; unlinking on an ambiguous error is not.
        raise SocketPathInUse(path) from None
    else:
        raise SocketPathInUse(path)
    finally:
        probe.close()


class UnixSocketServerMixin:
    """Bind a `socketserver.TCPServer` subclass to an AF_UNIX path."""

    address_family = socket.AF_UNIX
    #: Mode applied to the socket, fixed by P12. Caddy connects as a client and
    #: must be able to write; nothing else on the host may reach it.
    socket_mode = 0o660
    allow_reuse_address = False

    def server_bind(self) -> None:
        path = self.server_address
        clear_stale_socket(path)
        # Create the socket with the final mode rather than widening it after
        # bind: between bind() and chmod() a permissive umask would leave the
        # socket briefly connectable by any local user.
        previous = os.umask(0o777 & ~self.socket_mode)
        try:
            self.socket.bind(path)
        finally:
            os.umask(previous)
        os.chmod(path, self.socket_mode)
        self.server_address = path
        # Consumed by BaseHTTPRequestHandler for the Host fallback and by
        # `server_name`/`server_port` readers; neither is meaningful here.
        self.server_name = UNIX_PEER
        self.server_port = 0

    def get_request(self):
        connection, _ = self.socket.accept()
        return connection, (UNIX_PEER, 0)

    def server_close(self) -> None:
        super().server_close()
        try:
            os.unlink(self.server_address)
        except (FileNotFoundError, TypeError):
            pass
