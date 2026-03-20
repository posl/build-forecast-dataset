from __future__ import annotations

import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .models import CheckOutcome, ErrorReporter, Progress, Repo, RepoCheckError, Stats
from .parser import runtime_batch_size, take_batch


TRANSIENT_HTTP_CODES = {403, 429, 500, 502, 503, 504}


def parse_reset_at_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def retry_delay(headers: urllib.error.HTTPError | dict[str, str] | None, attempt: int) -> float:
    retry_after = None
    if headers is not None:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                pass

        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset:
            try:
                return max(float(reset) - time.time(), 1.0)
            except ValueError:
                pass

    return min(60.0, 2.0 ** attempt)


def log_retry(verbose: bool, method: str, url: str, attempt: int, retries: int, reason: str, delay: float) -> None:
    if not verbose:
        return
    print(
        f"[retry] {method} {url} attempt={attempt + 1}/{retries + 1} reason={reason} sleep={delay:.1f}s",
        file=sys.stderr,
    )


def post_json(
    url: str,
    payload: dict[str, object],
    headers: dict[str, str],
    timeout: float,
    retries: int,
    verbose: bool = False,
) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_CODES or attempt >= retries:
                raise
            delay = retry_delay(exc.headers, attempt)
            log_retry(verbose, "POST", url, attempt, retries, f"HTTP {exc.code}", delay)
            time.sleep(delay)
        except urllib.error.URLError as exc:
            if attempt >= retries:
                raise
            delay = retry_delay(None, attempt)
            log_retry(verbose, "POST", url, attempt, retries, f"URLError {exc.reason}", delay)
            time.sleep(delay)

    raise RuntimeError("unreachable")


def get_json(
    url: str,
    headers: dict[str, str],
    timeout: float,
    retries: int,
    verbose: bool = False,
) -> tuple[int, object | None]:
    request = urllib.request.Request(url, headers=headers, method="GET")

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
                return response.status, payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, None
            if exc.code == 451:
                return 451, None
            if exc.code not in TRANSIENT_HTTP_CODES or attempt >= retries:
                raise
            delay = retry_delay(exc.headers, attempt)
            log_retry(verbose, "GET", url, attempt, retries, f"HTTP {exc.code}", delay)
            time.sleep(delay)
        except urllib.error.URLError as exc:
            if attempt >= retries:
                raise
            delay = retry_delay(None, attempt)
            log_retry(verbose, "GET", url, attempt, retries, f"URLError {exc.reason}", delay)
            time.sleep(delay)

    raise RuntimeError("unreachable")


def head_status(
    url: str,
    headers: dict[str, str],
    timeout: float,
    retries: int,
    verbose: bool = False,
) -> int:
    request = urllib.request.Request(url, headers=headers, method="HEAD")

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404
            if exc.code == 451:
                return 451
            if exc.code not in TRANSIENT_HTTP_CODES or attempt >= retries:
                raise
            delay = retry_delay(exc.headers, attempt)
            log_retry(verbose, "HEAD", url, attempt, retries, f"HTTP {exc.code}", delay)
            time.sleep(delay)
        except urllib.error.URLError as exc:
            if attempt >= retries:
                raise
            delay = retry_delay(None, attempt)
            log_retry(verbose, "HEAD", url, attempt, retries, f"URLError {exc.reason}", delay)
            time.sleep(delay)

    raise RuntimeError("unreachable")


def check_batch_graphql(
    batch: Sequence[Repo],
    *,
    endpoint: str,
    token: str,
    timeout: float,
    retries: int,
    verbose: bool,
    error_reporter: ErrorReporter,
) -> CheckOutcome:
    lines = ["query CheckWorkflows {", "  rateLimit { cost remaining resetAt }"]
    aliases: list[str] = []

    for index, repo in enumerate(batch):
        owner, _, name = repo.full_name.partition("/")
        alias = f"r{index}"
        aliases.append(alias)
        lines.append(
            (
                f"  {alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                f"{{ object(expression: {json.dumps(repo.default_branch + ':.github/workflows')}) "
                "{ __typename } }"
            )
        )
    lines.append("}")

    try:
        payload = post_json(
            endpoint,
            {"query": "\n".join(lines)},
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "build-forecast-dataset-filter",
            },
            timeout,
            retries,
            verbose,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 451:
            if len(batch) == 1:
                repo = batch[0]
                reason = "GraphQL HTTP 451 Unavailable For Legal Reasons"
                error_reporter.report(repo, reason)
                return CheckOutcome(results=[], failed=[(repo, reason)])

            if verbose:
                print(
                    f"[warn] GraphQL HTTP 451 on batch of {len(batch)} repos; splitting batch to isolate repo",
                    file=sys.stderr,
                )

            middle = len(batch) // 2
            left = check_batch_graphql(
                batch[:middle],
                endpoint=endpoint,
                token=token,
                timeout=timeout,
                retries=retries,
                verbose=verbose,
                error_reporter=error_reporter,
            )
            right = check_batch_graphql(
                batch[middle:],
                endpoint=endpoint,
                token=token,
                timeout=timeout,
                retries=retries,
                verbose=verbose,
                error_reporter=error_reporter,
            )

            remaining_values = [value for value in (left.remaining, right.remaining) if value is not None]
            combined_remaining = min(remaining_values) if remaining_values else None
            combined_cost = None
            if left.cost is not None or right.cost is not None:
                combined_cost = (left.cost or 0) + (right.cost or 0)

            return CheckOutcome(
                results=left.results + right.results,
                failed=(left.failed or []) + (right.failed or []),
                remaining=combined_remaining,
                cost=combined_cost,
                reset_at=right.reset_at or left.reset_at,
            )

        reason = f"GraphQL HTTP {exc.code}: {exc.reason}"
        for repo in batch:
            error_reporter.report(repo, reason)
        return CheckOutcome(results=[], failed=[(repo, reason) for repo in batch])
    except urllib.error.URLError as exc:
        reason = f"GraphQL URLError: {exc.reason}"
        for repo in batch:
            error_reporter.report(repo, reason)
        return CheckOutcome(results=[], failed=[(repo, reason) for repo in batch])

    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GraphQL response does not contain a data object")

    rate_limit = data.get("rateLimit")
    remaining = None
    cost = None
    reset_at = None
    if isinstance(rate_limit, dict):
        remaining = rate_limit.get("remaining")
        cost = rate_limit.get("cost")
        reset_at = rate_limit.get("resetAt")

    results: list[tuple[Repo, bool]] = []
    for alias, repo in zip(aliases, batch, strict=True):
        node = data.get(alias)
        has_workflows = False
        if isinstance(node, dict):
            obj = node.get("object")
            if isinstance(obj, dict) and obj.get("__typename") == "Tree":
                has_workflows = True
        results.append((repo, has_workflows))
    return CheckOutcome(results=results, remaining=remaining, cost=cost, reset_at=reset_at)


def check_repo_rest(
    repo: Repo,
    *,
    endpoint: str,
    token: str,
    timeout: float,
    retries: int,
    verbose: bool,
    error_reporter: ErrorReporter,
) -> tuple[Repo, bool]:
    branch = urllib.parse.quote(repo.default_branch, safe="")
    url = f"{endpoint}/repos/{repo.full_name}/contents/.github/workflows?ref={branch}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "build-forecast-dataset-filter",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        status, payload = get_json(url, headers, timeout, retries, verbose)
    except urllib.error.HTTPError as exc:
        reason = f"REST HTTP {exc.code}: {exc.reason}"
        error_reporter.report(repo, reason)
        raise RepoCheckError(reason) from exc
    except urllib.error.URLError as exc:
        reason = f"REST URLError: {exc.reason}"
        error_reporter.report(repo, reason)
        raise RepoCheckError(reason) from exc

    if status == 451:
        reason = "REST HTTP 451 Unavailable For Legal Reasons"
        error_reporter.report(repo, reason)
        raise RepoCheckError(reason)

    if status == 404 or payload is None:
        return repo, False

    if isinstance(payload, list):
        return repo, True
    return repo, isinstance(payload, dict) and payload.get("type") == "dir"


def check_repo_head(
    repo: Repo,
    *,
    endpoint: str,
    timeout: float,
    retries: int,
    verbose: bool,
    error_reporter: ErrorReporter,
) -> tuple[Repo, bool]:
    branch = urllib.parse.quote(repo.default_branch, safe="")
    url = f"{endpoint}/{repo.full_name}/tree/{branch}/.github/workflows"
    try:
        status = head_status(
            url,
            {
                "User-Agent": "build-forecast-dataset-filter",
            },
            timeout,
            retries,
            verbose,
        )
    except urllib.error.HTTPError as exc:
        reason = f"HEAD HTTP {exc.code}: {exc.reason}"
        error_reporter.report(repo, reason)
        raise RepoCheckError(reason) from exc
    except urllib.error.URLError as exc:
        reason = f"HEAD URLError: {exc.reason}"
        error_reporter.report(repo, reason)
        raise RepoCheckError(reason) from exc

    if status == 451:
        reason = "HEAD HTTP 451 Unavailable For Legal Reasons"
        error_reporter.report(repo, reason)
        raise RepoCheckError(reason)

    return repo, status == 200


def handle_done(
    done: Iterable[concurrent.futures.Future[CheckOutcome]],
    pending: dict[concurrent.futures.Future[CheckOutcome], Sequence[Repo]],
    outfile,
    stats: Stats,
    progress: Progress,
    verbose: bool,
    graphql_min_remaining: int,
    graphql_limit_action: str,
    error_reporter: ErrorReporter,
) -> tuple[str | None, float | None]:
    limit_action = None
    resume_at_ts = None

    for future in done:
        try:
            outcome = future.result()
        except Exception as exc:
            batch = pending.get(future, ())
            reason = f"unexpected batch failure: {exc}"
            for repo in batch:
                error_reporter.report(repo, reason)
            stats.network_errors += max(len(batch), 1)
            if verbose:
                print(f"[warn] batch failed: {exc}", file=sys.stderr)
            continue

        for repo, has_workflows in outcome.results:
            stats.checked += 1
            if has_workflows:
                outfile.write(repo.url)
                outfile.write("\n")
                stats.matched += 1
            progress.log_checked(stats)

        for repo, reason in outcome.failed or []:
            stats.network_errors += 1
            if verbose:
                print(
                    f"[warn] failed repo={repo.full_name} branch={repo.default_branch} reason={reason}",
                    file=sys.stderr,
                )

        if outcome.remaining is not None and outcome.remaining <= graphql_min_remaining:
            limit_action = graphql_limit_action
            candidate_reset_at = parse_reset_at_timestamp(outcome.reset_at)
            if candidate_reset_at is not None:
                if resume_at_ts is None or candidate_reset_at > resume_at_ts:
                    resume_at_ts = candidate_reset_at
            if verbose:
                print(
                    (
                        f"[warn] GraphQL remaining={outcome.remaining} cost={outcome.cost} "
                        f"reset_at={outcome.reset_at}"
                    ),
                    file=sys.stderr,
                )
    return limit_action, resume_at_ts


def default_workers(mode: str) -> int:
    if mode == "graphql":
        return 32
    if mode == "head":
        return 64
    return 32


def default_batch_size(mode: str) -> int:
    return 64 if mode == "graphql" else 1


def process_candidates(
    repo_iter: Iterable[Repo],
    output_path: Path,
    stats: Stats,
    progress: Progress,
    *,
    mode: str,
    workers: int,
    batch_size: int,
    graphql_endpoint: str,
    rest_endpoint: str,
    head_endpoint: str,
    token: str,
    timeout: float,
    retries: int,
    verbose: bool,
    graphql_min_remaining: int,
    graphql_limit_action: str,
    graphql_resume_grace_seconds: int,
    error_reporter: ErrorReporter,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "none":
        with output_path.open("w", encoding="utf-8", buffering=1024 * 1024) as outfile:
            for repo in repo_iter:
                outfile.write(repo.url)
                outfile.write("\n")
                stats.checked += 1
                stats.matched += 1
                progress.log_checked(stats)
        return

    def make_checker(active_mode: str) -> Callable[[Sequence[Repo]], CheckOutcome]:
        if active_mode == "graphql":
            return lambda batch: check_batch_graphql(
                batch,
                endpoint=graphql_endpoint,
                token=token,
                timeout=timeout,
                retries=retries,
                verbose=verbose,
                error_reporter=error_reporter,
            )

        if active_mode == "head":
            def head_checker(batch: Sequence[Repo]) -> CheckOutcome:
                results: list[tuple[Repo, bool]] = []
                failed: list[tuple[Repo, str]] = []
                for repo in batch:
                    try:
                        results.append(
                            check_repo_head(
                                repo,
                                endpoint=head_endpoint,
                                timeout=timeout,
                                retries=retries,
                                verbose=verbose,
                                error_reporter=error_reporter,
                            )
                        )
                    except RepoCheckError as exc:
                        failed.append((repo, exc.reason))
                return CheckOutcome(results=results, failed=failed)

            return head_checker

        def rest_checker(batch: Sequence[Repo]) -> CheckOutcome:
            results: list[tuple[Repo, bool]] = []
            failed: list[tuple[Repo, str]] = []
            for repo in batch:
                try:
                    results.append(
                        check_repo_rest(
                            repo,
                            endpoint=rest_endpoint,
                            token=token,
                            timeout=timeout,
                            retries=retries,
                            verbose=verbose,
                            error_reporter=error_reporter,
                        )
                    )
                except RepoCheckError as exc:
                    failed.append((repo, exc.reason))
            return CheckOutcome(results=results, failed=failed)

        return rest_checker

    with (
        output_path.open("w", encoding="utf-8", buffering=1024 * 1024) as outfile,
        concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor,
    ):
        repo_iter = iter(repo_iter)
        active_mode = mode
        checker = make_checker(active_mode)
        pending: dict[concurrent.futures.Future[CheckOutcome], Sequence[Repo]] = {}
        stop_submitting = False
        pending_limit = workers if active_mode == "graphql" else workers * 2
        graphql_resume_at: float | None = None

        while True:
            if stop_submitting:
                break

            if (
                active_mode == "head"
                and graphql_limit_action == "fallback-head"
                and graphql_resume_at is not None
                and time.time() >= graphql_resume_at
            ):
                active_mode = "graphql_probe"
                checker = make_checker("graphql")
                pending_limit = 1
                graphql_resume_at = None
                if verbose:
                    print("info: probing GraphQL after rate-limit reset", file=sys.stderr)

            current_batch_size = runtime_batch_size(active_mode, batch_size)
            batch = take_batch(repo_iter, current_batch_size)
            if not batch:
                break

            future = executor.submit(checker, batch)
            pending[future] = batch

            if len(pending) >= pending_limit:
                done, _ = concurrent.futures.wait(
                    set(pending),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                action, reset_at_ts = handle_done(
                    done,
                    pending,
                    outfile,
                    stats,
                    progress,
                    verbose,
                    graphql_min_remaining,
                    graphql_limit_action,
                    error_reporter,
                )
                for future in done:
                    pending.pop(future, None)

                if action == "fallback-head":
                    graphql_resume_at = (
                        (reset_at_ts + graphql_resume_grace_seconds)
                        if reset_at_ts is not None
                        else (time.time() + graphql_resume_grace_seconds)
                    )
                    if verbose:
                        resume_at_iso = datetime.fromtimestamp(
                            graphql_resume_at, timezone.utc
                        ).isoformat()
                        print(
                            f"info: GraphQL resume probe scheduled at {resume_at_iso}",
                            file=sys.stderr,
                        )
                if action == "fallback-head" and active_mode in {"graphql", "graphql_probe"}:
                    active_mode = "head"
                    checker = make_checker(active_mode)
                    pending_limit = workers * 2
                    stats.fell_back_to_head = True
                    print(
                        "switching from GraphQL to HEAD mode to preserve GraphQL rate-limit budget",
                        file=sys.stderr,
                    )
                elif active_mode == "graphql_probe":
                    active_mode = "graphql"
                    checker = make_checker(active_mode)
                    pending_limit = workers
                    if verbose:
                        print("info: GraphQL probe succeeded; resuming GraphQL mode", file=sys.stderr)
                elif action == "stop":
                    stop_submitting = True
                    stats.stopped_early = True

        while pending:
            done, _ = concurrent.futures.wait(
                set(pending),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            action, reset_at_ts = handle_done(
                done,
                pending,
                outfile,
                stats,
                progress,
                verbose,
                graphql_min_remaining,
                graphql_limit_action,
                error_reporter,
            )
            for future in done:
                pending.pop(future, None)

            if action == "fallback-head":
                graphql_resume_at = (
                    (reset_at_ts + graphql_resume_grace_seconds)
                    if reset_at_ts is not None
                    else (time.time() + graphql_resume_grace_seconds)
                )
                if verbose:
                    resume_at_iso = datetime.fromtimestamp(
                        graphql_resume_at, timezone.utc
                    ).isoformat()
                    print(
                        f"info: GraphQL resume probe scheduled at {resume_at_iso}",
                        file=sys.stderr,
                    )
            if action == "fallback-head" and active_mode in {"graphql", "graphql_probe"}:
                active_mode = "head"
                checker = make_checker(active_mode)
                pending_limit = workers * 2
                stats.fell_back_to_head = True
                print(
                    "switching from GraphQL to HEAD mode to preserve GraphQL rate-limit budget",
                    file=sys.stderr,
                )
            elif active_mode == "graphql_probe":
                active_mode = "graphql"
                checker = make_checker(active_mode)
                pending_limit = workers
                if verbose:
                    print("info: GraphQL probe succeeded; resuming GraphQL mode", file=sys.stderr)
            elif action == "stop":
                stats.stopped_early = True

        if stats.stopped_early:
            print(
                "stopped early to preserve remaining GraphQL rate-limit budget",
                file=sys.stderr,
            )
