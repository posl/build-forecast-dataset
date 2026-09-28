import argparse
import mmap
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .parser import (
    ARRAY_END,
    BACKSLASH,
    BRANCH_MARKER,
    CLOSE_BRACE,
    IS_ARCHIVED_MARKER,
    IS_DISABLED_MARKER,
    IS_FORK_MARKER,
    IS_LOCKED_MARKER,
    ITEMS_MARKER,
    NAME_MARKER,
    OPEN_BRACE,
    QUOTE,
    WHITESPACE_AND_COMMA,
    extract_json_bool,
    extract_json_string,
)


@dataclass(slots=True)
class FilterCounts:
    total: int = 0
    excluded_inactive: int = 0
    excluded_archived: int = 0
    excluded_disabled: int = 0
    excluded_locked: int = 0
    remaining_after_active: int = 0
    excluded_fork: int = 0
    remaining_after_fork: int = 0
    invalid_or_missing_required_fields: int = 0
    matched_workflow: int | None = None
    workflow_check_errors: int | None = None
    excluded_no_workflow_or_unconfirmed: int | None = None


def count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as infile:
        for line in infile:
            if line.strip():
                count += 1
    return count


def count_filter_stages(input_path: Path, limit: int = 0) -> FilterCounts:
    counts = FilterCounts()
    scanned_limit = limit if limit > 0 else None

    with (
        input_path.open("rb") as infile,
        mmap.mmap(infile.fileno(), 0, access=mmap.ACCESS_READ) as mm,
    ):
        items_pos = mm.find(ITEMS_MARKER)
        if items_pos == -1:
            raise ValueError(f"{input_path} does not contain an items array")

        size = len(mm)
        pos = items_pos + len(ITEMS_MARKER)

        while pos < size:
            current = mm[pos]
            while current in WHITESPACE_AND_COMMA:
                pos += 1
                if pos >= size:
                    return counts
                current = mm[pos]

            if current == ARRAY_END:
                return counts
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
                            count_item(mm, start, end, counts)
                            pos = end
                            if (
                                scanned_limit is not None
                                and counts.total >= scanned_limit
                            ):
                                return counts
                            break
                pos += 1
            else:
                raise ValueError(
                    f"Unexpected end of file while parsing repository {counts.total + 1}"
                )

    return counts


def count_item(mm: mmap.mmap, start: int, end: int, counts: FilterCounts) -> None:
    counts.total += 1

    is_fork = extract_json_bool(mm, start, end, IS_FORK_MARKER)
    is_archived = extract_json_bool(mm, start, end, IS_ARCHIVED_MARKER)
    is_disabled = extract_json_bool(mm, start, end, IS_DISABLED_MARKER)
    is_locked = extract_json_bool(mm, start, end, IS_LOCKED_MARKER)

    if (
        is_fork is None
        or is_archived is None
        or is_disabled is None
        or is_locked is None
    ):
        counts.invalid_or_missing_required_fields += 1
        return

    if is_archived:
        counts.excluded_archived += 1
    if is_disabled:
        counts.excluded_disabled += 1
    if is_locked:
        counts.excluded_locked += 1

    if is_archived or is_disabled or is_locked:
        counts.excluded_inactive += 1
        return

    counts.remaining_after_active += 1

    if is_fork:
        counts.excluded_fork += 1
        return

    full_name = extract_json_string(mm, start, end, NAME_MARKER)
    default_branch = extract_json_string(mm, start, end, BRANCH_MARKER)
    if not full_name or "/" not in full_name or not default_branch:
        counts.invalid_or_missing_required_fields += 1
        return

    counts.remaining_after_fork += 1


def attach_workflow_counts(
    counts: FilterCounts,
    matched_output: Path | None,
    error_output: Path | None,
) -> None:
    if matched_output is not None:
        counts.matched_workflow = count_lines(matched_output)
    if error_output is not None:
        counts.workflow_check_errors = count_lines(error_output)

    if counts.matched_workflow is not None:
        errors = counts.workflow_check_errors or 0
        counts.excluded_no_workflow_or_unconfirmed = (
            counts.remaining_after_fork - counts.matched_workflow - errors
        )


def format_text(counts: FilterCounts) -> str:
    lines = [
        f"total: {counts.total:,}",
        f"excluded_inactive: {counts.excluded_inactive:,}",
        f"  archived: {counts.excluded_archived:,}",
        f"  disabled: {counts.excluded_disabled:,}",
        f"  locked: {counts.excluded_locked:,}",
        f"remaining_after_active: {counts.remaining_after_active:,}",
        f"excluded_fork: {counts.excluded_fork:,}",
        f"remaining_after_fork: {counts.remaining_after_fork:,}",
        (
            "invalid_or_missing_required_fields: "
            f"{counts.invalid_or_missing_required_fields:,}"
        ),
    ]

    if counts.matched_workflow is not None:
        lines.append(f"matched_workflow: {counts.matched_workflow:,}")
    if counts.workflow_check_errors is not None:
        lines.append(f"workflow_check_errors: {counts.workflow_check_errors:,}")
    if counts.excluded_no_workflow_or_unconfirmed is not None:
        lines.append(
            "excluded_no_workflow_or_unconfirmed: "
            f"{counts.excluded_no_workflow_or_unconfirmed:,}"
        )

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Count how many repositories are excluded at each local filtering stage. "
            "Workflow-stage counts can be derived from existing output files."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("input/repositories.json"),
        help="Path to the downloaded repositories.json file.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after scanning this many repositories.",
    )
    parser.add_argument(
        "--matched-output",
        type=Path,
        default=None,
        help="Optional repos.txt file from the workflow-checking pipeline.",
    )
    parser.add_argument(
        "--error-output",
        type=Path,
        default=None,
        help="Optional http_errors.txt file from the workflow-checking pipeline.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print counts as JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    counts = count_filter_stages(args.input, args.limit)
    attach_workflow_counts(counts, args.matched_output, args.error_output)

    if args.json:
        import json

        print(json.dumps(asdict(counts), ensure_ascii=False, indent=2))
    else:
        print(format_text(counts))

    if counts.excluded_no_workflow_or_unconfirmed is not None:
        print(
            (
                "note: excluded_no_workflow_or_unconfirmed assumes the matched/error "
                "files were produced from the same input and limit."
            ),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
