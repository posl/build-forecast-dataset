from __future__ import annotations

import sys
import time

from .cli import get_github_token, parse_args
from .models import ErrorReporter, Progress, Stats
from .parser import iter_candidate_repos
from .pipeline import default_batch_size, default_workers, process_candidates


def resolve_mode(check_mode: str, token: str) -> str:
    if check_mode == "auto":
        return "graphql" if token else "rest"
    if check_mode == "graphql" and not token:
        raise SystemExit("--check graphql requires --token or GITHUB_TOKEN")
    return check_mode


def main() -> int:
    args = parse_args()
    token = args.token or get_github_token()
    mode = resolve_mode(args.check, token)
    workers = args.workers if args.workers > 0 else default_workers(mode)
    batch_size = args.batch_size if args.batch_size > 0 else default_batch_size(mode)

    if token:
        print("info: GITHUB_TOKEN loaded", file=sys.stderr)
    if args.verbose:
        print(
            (
                f"info: verbose mode enabled mode={mode} workers={workers} "
                f"batch_size={batch_size} retries={args.retries}"
            ),
            file=sys.stderr,
        )

    if workers <= 0:
        raise SystemExit("--workers must be positive")
    if batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if mode == "graphql" and not token:
        raise SystemExit("GraphQL mode requires a GitHub token")

    started_at = time.time()
    stats = Stats(started_at=started_at)
    progress = Progress(args.progress_every, started_at)
    error_reporter = ErrorReporter(args.error_output)

    if mode == "rest" and not token:
        print(
            "warning: REST mode without GITHUB_TOKEN is rate-limited by GitHub and will be very slow",
            file=sys.stderr,
        )

    repo_iter = iter_candidate_repos(args.input, stats, progress, args.limit)
    process_candidates(
        repo_iter,
        args.output,
        stats,
        progress,
        mode=mode,
        workers=workers,
        batch_size=batch_size,
        graphql_endpoint=args.graphql_endpoint,
        rest_endpoint=args.rest_endpoint.rstrip("/"),
        head_endpoint=args.head_endpoint.rstrip("/"),
        token=token,
        timeout=args.timeout,
        retries=args.retries,
        verbose=args.verbose,
        graphql_min_remaining=args.graphql_min_remaining,
        graphql_limit_action=args.graphql_limit_action,
        graphql_resume_grace_seconds=args.graphql_resume_grace_seconds,
        error_reporter=error_reporter,
    )

    if error_reporter.count() > 0:
        error_reporter.write()
        print(
            f"info: wrote {error_reporter.count():,} HTTP/network error records to {args.error_output}",
            file=sys.stderr,
        )

    elapsed = max(time.time() - started_at, 0.001)
    print(
        (
            f"done: scanned={stats.scanned:,} eligible={stats.eligible:,} checked={stats.checked:,} "
            f"matched={stats.matched:,} errors={stats.network_errors:,} "
            f"stopped_early={stats.stopped_early} fell_back_to_head={stats.fell_back_to_head} "
            f"elapsed={elapsed:,.1f}s"
        ),
        file=sys.stderr,
    )
    return 0
