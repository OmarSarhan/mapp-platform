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
        choices=("init", "reset-password", "reset-demo", "revoke-tokens"),
    )
    parser.add_argument("--root", default=os.environ.get("CONTROL_DIR", "/control"))
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the administrator password from standard input.",
    )
    return parser


def main() -> None:
    args = parser().parse_args()
    store = ControlStore(Path(args.root))
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
