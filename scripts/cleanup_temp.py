#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
from pathlib import Path


RETENTION_DAYS = 7
ARTIFACT_RUN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z)-"
    r"[A-Za-z0-9._-]+-[0-9a-f]{8}$"
)
TEMP_FILE = re.compile(
    r"^(?:\.[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]{6,}\.tmp|"
    r"\.workspace-[A-Za-z0-9_-]+\.json)$"
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _artifact_time(name: str) -> dt.datetime | None:
    match = ARTIFACT_RUN.fullmatch(name)
    if match is None:
        return None
    try:
        parsed = dt.datetime.strptime(
            match.group("timestamp"),
            "%Y-%m-%dT%H-%M-%S-%fZ",
        )
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc)


def _safe_artifact_tree(path: Path) -> bool:
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in directories:
            metadata = (root_path / name).lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                return False
        for name in files:
            metadata = (root_path / name).lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return False
    return True


def _remove_tree_descriptor(descriptor: int) -> None:
    for entry in os.scandir(descriptor):
        if entry.is_dir(follow_symlinks=False):
            child = os.open(
                entry.name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=descriptor,
            )
            try:
                _remove_tree_descriptor(child)
            finally:
                os.close(child)
            os.rmdir(entry.name, dir_fd=descriptor)
        elif entry.is_file(follow_symlinks=False):
            os.unlink(entry.name, dir_fd=descriptor)
        else:
            raise OSError("Disposable tree changed during cleanup.")


def _remove_tree(path: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags)
    try:
        _remove_tree_descriptor(descriptor)
    finally:
        os.close(descriptor)


def cleanup_candidates(
    state_dir: Path,
    *,
    now: dt.datetime | None = None,
) -> list[Path]:
    state = state_dir.resolve(strict=True)
    metadata = state_dir.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("State directory must be a real directory.")

    current = now or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Current time must include a timezone.")
    cutoff = current.astimezone(dt.timezone.utc) - dt.timedelta(
        days=RETENTION_DAYS
    )
    candidates: list[Path] = []

    artifacts = state / "control" / "artifacts"
    if artifacts.is_dir() and not artifacts.is_symlink():
        for run in artifacts.iterdir():
            created = _artifact_time(run.name)
            if (
                created is not None
                and created < cutoff
                and run.is_dir()
                and not run.is_symlink()
                and _safe_artifact_tree(run)
            ):
                candidates.append(run)

    for root, directories, files in os.walk(state, followlinks=False):
        root_path = Path(root)
        directories[:] = [
            name
            for name in directories
            if not (root_path / name).is_symlink()
            and (root_path / name) != artifacts
        ]
        for name in files:
            if TEMP_FILE.fullmatch(name) is None:
                continue
            path = root_path / name
            file_metadata = path.lstat()
            modified = dt.datetime.fromtimestamp(
                file_metadata.st_mtime,
                tz=dt.timezone.utc,
            )
            if stat.S_ISREG(file_metadata.st_mode) and modified < cutoff:
                candidates.append(path)

    return sorted(candidates, key=lambda path: path.relative_to(state).as_posix())


def cleanup(state_dir: Path, *, confirm: bool) -> dict[str, object]:
    state = state_dir.resolve(strict=True)
    candidates = cleanup_candidates(state)
    relative = [path.relative_to(state).as_posix() for path in candidates]
    if confirm:
        for path in candidates:
            if path.is_dir() and not path.is_symlink():
                _remove_tree(path)
                path.rmdir()
            else:
                path.unlink()
    return {
        "retentionDays": RETENTION_DAYS,
        "confirmed": confirm,
        "candidateCount": len(relative),
        "candidates": relative,
        "removedCount": len(relative) if confirm else 0,
        "preserved": [
            "workspace",
            "authentication",
            "audit",
            "proposals",
            "semantic database and history",
            "reload state",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List or remove visual-test artifacts and incomplete atomic "
            "temporary files older than seven days."
        )
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "var",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Remove the listed files. Without this flag the command is a dry run.",
    )
    arguments = parser.parse_args()
    try:
        result = cleanup(arguments.state_dir, confirm=arguments.confirm)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
