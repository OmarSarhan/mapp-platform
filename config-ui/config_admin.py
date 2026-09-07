from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from control_plane import ControlStore


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "init",
            "reset-password",
            "reset-demo",
            "revoke-tokens",
            "mcp-client-register",
            "mcp-client-list",
            "mcp-client-disable",
        ),
    )
    parser.add_argument("--root", default=os.environ.get("CONTROL_DIR", "/control"))
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the administrator password from standard input.",
    )
    parser.add_argument("--name", help="Display name for an MCP client.")
    parser.add_argument(
        "--redirect-uri",
        action="append",
        default=[],
        metavar="URI",
        help="Exact redirect URI an MCP client may use. Repeatable.",
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help="Scope an MCP client may request. Repeatable.",
    )
    parser.add_argument("--client-id", help="MCP client to disable.")
    return parser


def mcp_client_command(args, store) -> bool:
    """The agent-client registry. Returns True when it handled the command.

    Registration is an operator act by design: an agent client is a third
    party asking for consent, and P2 requires one pinned client per ecosystem.
    RFC 7591 dynamic registration would remove the person from that decision.
    """
    if args.command == "mcp-client-list":
        clients = store.list_oauth_clients()
        if not clients:
            print(
                "No OAuth clients are registered, so the authorization"
                " component will refuse every authorization request."
            )
            return True
        for client in clients:
            state = "disabled" if client["disabled"] else "active"
            kind = "confidential" if client["confidential"] else "public"
            print(f"{client['clientId']}  {state}  {kind}  {client['name']}")
            print(f"    scopes:   {' '.join(client['scopes']) or '(none)'}")
            print(f"    redirect: {' '.join(client['redirectUris']) or '(none)'}")
        return True
    if args.command == "mcp-client-disable":
        if not args.client_id:
            raise SystemExit("--client-id is required.")
        try:
            disabled = store.disable_oauth_client(args.client_id)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        if disabled:
            print(f"Client {args.client_id} disabled.")
        else:
            print(f"Client {args.client_id} is unknown or already disabled.")
        return True
    if args.command == "mcp-client-register":
        try:
            client_id = store.register_oauth_client(
                name=args.name or "",
                redirect_uris=args.redirect_uri,
                scopes=args.scope,
            )
        except ValueError as exc:
            # An operator command, so a rejected argument is a message and an
            # exit code, not a traceback.
            raise SystemExit(str(exc)) from None
        # No secret is printed because none exists: an agent is a public
        # client and authenticates with PKCE alone.
        print(f"Registered public MCP client: {client_id}")
        print(f"    name:     {args.name}")
        print(f"    scopes:   {' '.join(args.scope)}")
        print(f"    redirect: {' '.join(args.redirect_uri)}")
        return True
    return False


def main() -> None:
    args = parser().parse_args()
    store = ControlStore(Path(args.root))
    if mcp_client_command(args, store):
        return
    if args.command == "revoke-tokens":
        store.revoke_all()
        print("All CLI tokens revoked.")
        return
    password = (
        sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else secrets.token_urlsafe(18)
    )
    if args.command == "init":
        if not store.initialize(password):
            print("Authentication already initialized; existing credentials were unchanged.")
            return
    else:
        store.reset_password(password, revoke_tokens=args.command == "reset-demo")
    print(f"Admin password (shown once): {password}")


if __name__ == "__main__":
    main()
