from __future__ import annotations

import argparse
import os
import secrets
import tempfile
from pathlib import Path


def keys(path: Path) -> set[str]:
    output: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        output.add(line.split("=", 1)[0].strip())
    return output


def missing_assignment_lines(example: Path, present: set[str]) -> list[str]:
    output: list[str] = []
    for raw in example.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        secret_placeholder = (
            any(marker in key for marker in ("PASSWORD", "SECRET", "TOKEN"))
            and (not value or "CHANGEME" in value)
        )
        if key not in present:
            output.append(
                f"{key}={secrets.token_hex(24)}"
                if secret_placeholder
                else line
            )
    return output


def add_missing_defaults(example: Path, environment: Path, present: set[str]) -> int:
    additions = missing_assignment_lines(example, present)
    if not additions:
        return 0
    current = environment.read_text(encoding="utf-8")
    suffix = "\n" if current.endswith("\n") else "\n\n"
    updated = (
        current
        + suffix
        + "# Added from .env.example by ./bin/mapp doctor --add-missing.\n"
        + "\n".join(additions)
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env-",
        dir=environment.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, environment)
    finally:
        if temporary.exists():
            temporary.unlink()
    return len(additions)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare environment keys without reading or printing values."
    )
    parser.add_argument("--example", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument(
        "--add-missing",
        action="store_true",
        help="append missing assignments from the example without changing existing values",
    )
    args = parser.parse_args()

    if not args.example.is_file():
        parser.error(f"example file does not exist: {args.example}")
    if not args.environment.is_file():
        print(f"Environment file does not exist: {args.environment}")
        print("Run ./bin/mapp init first.")
        return 2

    expected = keys(args.example)
    actual = keys(args.environment)
    missing = sorted(expected - actual)
    obsolete = sorted(actual - expected)
    if args.add_missing and missing:
        added = add_missing_defaults(args.example, args.environment, actual)
        if added:
            print(
                f"Added {added} missing assignments from .env.example; "
                "secret placeholders were generated securely."
            )
        actual = keys(args.environment)
        missing = sorted(expected - actual)
    if missing:
        print("Missing keys:")
        for key in missing:
            print(f"  {key}")
    if obsolete:
        print("Keys not present in .env.example:")
        for key in obsolete:
            print(f"  {key}")
    if not missing and not obsolete:
        print("Environment keys match .env.example.")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
