import argparse
import os
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
DEFAULT_REST_ENDPOINT = "https://api.github.com"
DEFAULT_HEAD_ENDPOINT = "https://github.com"
DEFAULT_OUTPUT = Path("output/repos.txt")
DEFAULT_ERROR_OUTPUT = Path("output/http_errors.txt")
DEFAULT_RETRY_OUTPUT = Path("output/repos_retry.txt")
DEFAULT_RETRY_ERROR_OUTPUT = Path("output/http_errors_retry.txt")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Filter repositories from a GitHub Search Project JSON dump. "
            "The parser is streaming and does not load the full input into memory."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("input/repositories.json"),
        help="Path to the downloaded repositories.json file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Path to the output file containing matching GitHub repository URLs. "
            "Defaults to output/repos.txt, or output/repos_retry.txt when --retry-errors-input is used."
        ),
    )
    parser.add_argument(
        "--error-output",
        type=Path,
        default=None,
        help=(
            "Path to the output file listing repositories that could not be checked due to HTTP/network errors. "
            "Defaults to output/http_errors.txt, or output/http_errors_retry.txt when --retry-errors-input is used."
        ),
    )
    parser.add_argument(
        "--retry-errors-input",
        type=Path,
        default=None,
        help=(
            "Retry only repositories listed in an existing HTTP/network error file. "
            "Expected format: full_name<TAB>default_branch<TAB>reason."
        ),
    )
    parser.add_argument(
        "--check",
        choices=("auto", "graphql", "rest", "head", "none"),
        default="auto",
        help=(
            "How to verify .github/workflows. "
            "'auto' uses GraphQL when GITHUB_TOKEN is set, otherwise REST."
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub token. Defaults to GITHUB_TOKEN.",
    )
    parser.add_argument(
        "--graphql-endpoint",
        default=DEFAULT_GRAPHQL_ENDPOINT,
        help="GraphQL endpoint for batched workflow checks.",
    )
    parser.add_argument(
        "--rest-endpoint",
        default=DEFAULT_REST_ENDPOINT,
        help="REST API endpoint for per-repository workflow checks.",
    )
    parser.add_argument(
        "--head-endpoint",
        default=DEFAULT_HEAD_ENDPOINT,
        help="Base GitHub web endpoint for HEAD workflow checks.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Worker count. Defaults to 8 for GraphQL, 16 for HEAD, and 8 for REST.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Repositories per request batch. Defaults to 32 for GraphQL, 4 for REST, and 1 for HEAD.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Per-request timeout in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Retry count for transient API failures.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after scanning this many repositories. Useful for dry runs and benchmarks.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=50000,
        help="Emit a progress line every N scanned or checked repositories. Set 0 to disable.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print retry/backoff diagnostics and per-repo failure details.",
    )
    parser.add_argument(
        "--graphql-min-remaining",
        type=int,
        default=250,
        help="GraphQL remaining-points floor used by the configured limit action.",
    )
    parser.add_argument(
        "--graphql-limit-action",
        choices=("fallback-head", "stop"),
        default="fallback-head",
        help="What to do when GraphQL remaining points reach the configured floor.",
    )
    parser.add_argument(
        "--graphql-resume-grace-seconds",
        type=int,
        default=30,
        help="Extra delay after GraphQL reset time before probing GraphQL again.",
    )
    return parser.parse_args()


def get_github_token() -> str:
    load_dotenv()
    return os.getenv("GITHUB_TOKEN") or ""
