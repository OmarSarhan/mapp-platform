"""The deployed entry point: one ASGI worker on a Unix socket.

A socket rather than a port, for the same reason mcp-auth uses one: the only
peer is Caddy, so there is nothing to reach the runtime from the network even if
a route were misconfigured. P12 fixes the path and the ownership, and compose
supplies the uid and gid -- the process does not chown anything, because a
process that can change the ownership of its own socket can change other things.

One worker, also from P12. Several would each hold their own introspection cache
and their own view of which credentials are live, so a revocation would take
effect on one and not the others.
"""

from __future__ import annotations

import os
import socket
import sys

import uvicorn

from app import build_app

#: 0660: Caddy and this process share a group, and nothing else on the host has
#: any business speaking to it.
SOCKET_MODE = 0o660


def main() -> int:
    socket_path = os.environ.get("MCP_RUNTIME_SOCKET", "/run/mapp-mcp/mapp-mcp.sock")
    # A socket file outlives the process that made it, so a container that was
    # killed rather than stopped leaves one behind and bind() then fails with
    # "address already in use" -- an error that reads like a port conflict and
    # is not one.
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    application, resource = build_app()

    # Bound here rather than by uvicorn, which hardcodes uds_perms = 0o666 and
    # chmods the socket to it. World-writable is the wrong mode for a socket
    # whose only legitimate peer is Caddy: anything that gets a foothold in a
    # container sharing the volume could speak to the runtime directly and skip
    # the edge. Binding it ourselves and handing over the descriptor is the only
    # way to keep P12's 0660.
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(socket_path)
    os.chmod(socket_path, SOCKET_MODE)
    listener.listen(128)

    print(
        f"mapp-mcp serving {resource.resource} on {socket_path}",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(
        application,
        fd=listener.fileno(),
        workers=1,
        access_log=False,
        # The component answers its own errors; uvicorn's default handler would
        # render an HTML page for a JSON-RPC client. Raisable without a rebuild,
        # because the first thing wanted when this misbehaves is the traceback
        # it is currently swallowing.
        log_level=os.environ.get("MCP_RUNTIME_LOG_LEVEL", "warning"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
