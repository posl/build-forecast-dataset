from __future__ import annotations

import json
import mmap
from pathlib import Path
from typing import Iterator

from .models import Progress, Repo, Stats


ITEMS_MARKER = b'"items":['
NAME_MARKER = b'"name":"'
BRANCH_MARKER = b'"defaultBranch":"'
IS_FORK_MARKER = b'"isFork":'
IS_ARCHIVED_MARKER = b'"isArchived":'
IS_DISABLED_MARKER = b'"isDisabled":'
IS_LOCKED_MARKER = b'"isLocked":'

QUOTE = ord('"')
BACKSLASH = ord("\\")
OPEN_BRACE = ord("{")
CLOSE_BRACE = ord("}")
ARRAY_END = ord("]")
WHITESPACE_AND_COMMA = b" \t\r\n,"


def decode_json_string(raw: bytes) -> str:
    if BACKSLASH not in raw:
        return raw.decode("utf-8")
    return json.loads((b'"' + raw + b'"').decode("utf-8"))


def extract_json_string(mm: mmap.mmap, start: int, end: int, marker: bytes) -> str | None:
    pos = mm.find(marker, start, end)
    if pos == -1:
        return None

    pos += len(marker)
    value_start = pos
    escaped = False

    while pos < end:
        current = mm[pos]
        if escaped:
            escaped = False
        elif current == BACKSLASH:
            escaped = True
        elif current == QUOTE:
            return decode_json_string(mm[value_start:pos])
        pos += 1

    return None


def extract_json_bool(mm: mmap.mmap, start: int, end: int, marker: bytes) -> bool | None:
    pos = mm.find(marker, start, end)
    if pos == -1:
        return None
    pos += len(marker)

    if mm[pos : pos + 4] == b"true":
        return True
    if mm[pos : pos + 5] == b"false":
        return False
    return None


def extract_repo(mm: mmap.mmap, start: int, end: int) -> Repo | None:
    is_fork = extract_json_bool(mm, start, end, IS_FORK_MARKER)
    is_archived = extract_json_bool(mm, start, end, IS_ARCHIVED_MARKER)
    is_disabled = extract_json_bool(mm, start, end, IS_DISABLED_MARKER)
    is_locked = extract_json_bool(mm, start, end, IS_LOCKED_MARKER)

    if (
        is_fork is None
        or is_archived is None
        or is_disabled is None
        or is_locked is None
        or is_fork
        or is_archived
        or is_disabled
        or is_locked
    ):
        return None

    full_name = extract_json_string(mm, start, end, NAME_MARKER)
    default_branch = extract_json_string(mm, start, end, BRANCH_MARKER)

    if not full_name or not default_branch or "/" not in full_name:
        return None

    return Repo(full_name=full_name, default_branch=default_branch)


def iter_candidate_repos(
    input_path: Path,
    stats: Stats,
    progress: Progress,
    limit: int,
) -> Iterator[Repo]:
    with input_path.open("rb") as infile, mmap.mmap(infile.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        items_pos = mm.find(ITEMS_MARKER)
        if items_pos == -1:
            raise ValueError(f"{input_path} does not contain an items array")

        size = len(mm)
        pos = items_pos + len(ITEMS_MARKER)
        scanned_limit = limit if limit > 0 else None

        while pos < size:
            current = mm[pos]
            while current in WHITESPACE_AND_COMMA:
                pos += 1
                if pos >= size:
                    return
                current = mm[pos]

            if current == ARRAY_END:
                return
            if current != OPEN_BRACE:
                raise ValueError(f"Unexpected JSON token at byte offset {pos}")

            start = pos
            depth = 0
            in_string = False
            escaped = False

            while pos < size:
                current = mm[pos]
                if in_string:
                    if escaped:
                        escaped = False
                    elif current == BACKSLASH:
                        escaped = True
                    elif current == QUOTE:
                        in_string = False
                else:
                    if current == QUOTE:
                        in_string = True
                    elif current == OPEN_BRACE:
                        depth += 1
                    elif current == CLOSE_BRACE:
                        depth -= 1
                        if depth == 0:
                            end = pos + 1
                            stats.scanned += 1
                            repo = extract_repo(mm, start, end)
                            if repo is not None:
                                stats.eligible += 1
                                yield repo
                            progress.log_scanned(stats)
                            pos = end
                            if scanned_limit is not None and stats.scanned >= scanned_limit:
                                return
                            break
                pos += 1
            else:
                raise ValueError(f"Unexpected end of file while parsing repository {stats.scanned + 1}")


def iter_repos_from_error_file(
    input_path: Path,
    stats: Stats,
    progress: Progress,
    limit: int,
) -> Iterator[Repo]:
    seen: set[tuple[str, str]] = set()
    scanned_limit = limit if limit > 0 else None

    with input_path.open("r", encoding="utf-8") as infile:
        for line_number, line in enumerate(infile, start=1):
            stats.scanned += 1

            stripped = line.rstrip("\n")
            if not stripped:
                progress.log_scanned(stats)
                if scanned_limit is not None and stats.scanned >= scanned_limit:
                    return
                continue

            parts = stripped.split("\t", 2)
            if len(parts) < 2:
                raise ValueError(
                    f"Malformed error input at line {line_number}: expected at least 2 tab-separated columns"
                )

            full_name = parts[0].strip()
            default_branch = parts[1].strip()
            key = (full_name, default_branch)
            if full_name and default_branch and "/" in full_name and key not in seen:
                seen.add(key)
                stats.eligible += 1
                yield Repo(full_name=full_name, default_branch=default_branch)

            progress.log_scanned(stats)
            if scanned_limit is not None and stats.scanned >= scanned_limit:
                return


def take_batch(iterator: Iterator[Repo], size: int) -> list[Repo]:
    batch: list[Repo] = []
    while len(batch) < size:
        try:
            batch.append(next(iterator))
        except StopIteration:
            break
    return batch


def runtime_batch_size(active_mode: str, configured_batch_size: int) -> int:
    if active_mode == "graphql":
        return configured_batch_size
    if active_mode == "graphql_probe":
        return 1
    return 1
