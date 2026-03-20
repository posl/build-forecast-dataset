from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Repo:
    full_name: str
    default_branch: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"


@dataclass(slots=True)
class Stats:
    started_at: float
    scanned: int = 0
    eligible: int = 0
    checked: int = 0
    matched: int = 0
    network_errors: int = 0
    stopped_early: bool = False
    fell_back_to_head: bool = False


class Progress:
    def __init__(self, every: int, start_time: float) -> None:
        self.every = every
        self.start_time = start_time
        self._lock = threading.Lock()
        self._next_scanned = every if every > 0 else None
        self._next_checked = every if every > 0 else None

    def log_scanned(self, stats: Stats) -> None:
        if self._next_scanned is None or stats.scanned < self._next_scanned:
            return
        with self._lock:
            while self._next_scanned is not None and stats.scanned >= self._next_scanned:
                elapsed = max(time.time() - self.start_time, 0.001)
                rate = stats.scanned / elapsed
                print(
                    (
                        f"[scan] scanned={stats.scanned:,} eligible={stats.eligible:,} "
                        f"checked={stats.checked:,} pending={stats.eligible - stats.checked:,} "
                        f"rate={rate:,.0f} repos/s"
                    ),
                    file=sys.stderr,
                )
                self._next_scanned += self.every

    def log_checked(self, stats: Stats) -> None:
        if self._next_checked is None or stats.checked < self._next_checked:
            return
        with self._lock:
            while self._next_checked is not None and stats.checked >= self._next_checked:
                elapsed = max(time.time() - self.start_time, 0.001)
                rate = stats.checked / elapsed
                print(
                    (
                        f"[check] scanned={stats.scanned:,} eligible={stats.eligible:,} "
                        f"checked={stats.checked:,} pending={stats.eligible - stats.checked:,} "
                        f"matched={stats.matched:,} errors={stats.network_errors:,} "
                        f"rate={rate:,.0f} repos/s"
                    ),
                    file=sys.stderr,
                )
                self._next_checked += self.every


@dataclass(slots=True)
class CheckOutcome:
    results: list[tuple[Repo, bool]]
    failed: list[tuple[Repo, str]] | None = None
    remaining: int | None = None
    cost: int | None = None
    reset_at: str | None = None


class ErrorReporter:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str, str]] = set()
        self._records: list[tuple[str, str, str]] = []

    def report(self, repo: Repo, reason: str) -> None:
        entry = (repo.full_name, repo.default_branch, reason)
        with self._lock:
            if entry in self._seen:
                return
            self._seen.add(entry)
            self._records.append(entry)
        print(
            f"[error] repo={repo.full_name} branch={repo.default_branch} reason={reason}",
            file=sys.stderr,
        )

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def write(self) -> None:
        with self._lock:
            records = list(self._records)

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("w", encoding="utf-8") as outfile:
            for full_name, default_branch, reason in records:
                outfile.write(f"{full_name}\t{default_branch}\t{reason}\n")


class RepoCheckError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
