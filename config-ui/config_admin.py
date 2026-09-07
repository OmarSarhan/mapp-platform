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
            "migrate-rollback",
            "advance-recovery-epoch",
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
    parser.add_argument(
        "--to",
        type=int,
        help="Schema version to roll the migration ladder back to. 0 removes"
        " every control table.",
    )
    parser.add_argument(
        "--reason",
        help="Recorded in the audit entry for an epoch advance.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Perform a rollback. Without it the plan is printed and nothing"
        " changes.",
    )
    return parser


def advance_recovery_epoch_command(args, store) -> bool:
    """Invalidate every credential the restored database brought back.

    A snapshot contains every credential that was valid when it was taken,
    including ones revoked since, so restoring it hands them back. This is the
    step that closes that -- and it is destructive in the ordinary way rather
    than the alarming way: nothing is lost that cannot be obtained again by
    signing in and consenting.

    Confirmed, because it is equally usable as a panic button on a live
    platform, where the effect is the same and the intent is different.
    """
    if args.command != "advance-recovery-epoch":
        return False
    if not args.confirm:
        print(f"Current recovery epoch: {store.recovery_epoch()}.")
        print()
        print(
            "Advancing it invalidates every credential this database now"
            " holds:"
        )
        print("  - dashboard sessions, so operators sign in again")
        print("  - CLI API tokens, so each has to be reissued")
        print("  - device authorizations in flight")
        print("  - MCP grants and their tokens, so agents consent again")
        print()
        print(
            "Not touched: the administrator credential, registered OAuth"
            " clients,\nthe audit log, and everything under var. Nothing here"
            " is unrecoverable --\nit all comes back by signing in and"
            " consenting again."
        )
        print()
        print(
            "Run this after restoring a database and before serving traffic,"
            " or a\npre-restore credential stays usable. Re-run with --confirm."
        )
        raise SystemExit(2)
    result = store.advance_recovery_epoch(reason=args.reason or "operator")
    print(f"Recovery epoch is now {result['epoch']}.")
    for table, count in sorted(result["revoked"].items()):
        if count:
            print(f"  revoked {count} in {table}")
    for table, count in sorted(result["deleted"].items()):
        if count:
            print(f"  removed {count} in {table}")
    if not any(result["revoked"].values()) and not any(result["deleted"].values()):
        print("  nothing was live, so nothing was invalidated")
    return True


def migrate_rollback_command(args, store) -> bool:
    """Step the schema ladder down, after saying what that costs.

    Two calls on purpose. Without --confirm this prints the plan and exits
    non-zero, which is the same shape reset-system uses: an operator reaching
    for a destructive command under pressure should read the price before
    paying it. The plan is computed from the ledger rather than written out, so
    it cannot go stale when a migration is added.
    """
    if args.command != "migrate-rollback":
        return False
    if args.to is None:
        raise SystemExit("--to is required: the schema version to roll back to.")
    plan = store.rollback_plan(args.to)
    if plan["missing"]:
        raise SystemExit(
            f"Migrations {plan['missing']} have no rollback, so the ladder"
            f" cannot step below {max(plan['missing'])}."
        )
    if not plan["undo"]:
        print(
            f"Schema is already at or below version {args.to};"
            f" applied: {plan['applied']}. Nothing to do."
        )
        return True
    if not args.confirm:
        print(
            f"Would roll back migrations {plan['undo']}"
            f" (applied: {plan['applied']})."
        )
        if plan["losses"]:
            print()
            print("This destroys, and no transaction brings it back:")
            for version in plan["undo"]:
                if version in plan["losses"]:
                    print(f"  migration {version}: {plan['losses'][version]}")
        print()
        print(
            "Not touched: the audit log, workspace proposals, artifacts and"
            " public assets under var, and the source databases holding the"
            " spatial data. Only the control schema is changed."
        )
        print()
        print(
            "Back up the database first if any of that matters, then"
            " re-run with --confirm."
        )
        raise SystemExit(2)
    undone = store.rollback_schema(args.to, accept_data_loss=True)
    print(f"Rolled back migrations {undone}. Schema is now at version {args.to}.")
    print(
        "The next start applies the forward ladder again, so re-running a"
        " migration is how you go back up."
    )
    return True


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
    if advance_recovery_epoch_command(args, store):
        return
    if migrate_rollback_command(args, store):
        return
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
