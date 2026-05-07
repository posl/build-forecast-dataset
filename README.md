# Dataset Creation

This repository filters repositories from a GitHub Search Project JSON export and writes matching repository URLs to a text file.

The expected input is the downloaded repository artifact from [SEART GitHub Search](https://seart-ghs.si.usi.ch/).

The main use case is:

1. Read a large `repositories.json` dump without loading the whole file into memory.
2. Exclude repositories that are not suitable candidates.
3. Check whether each remaining repository contains `.github/workflows`.
4. Write matching repository URLs to an output file.

## What It Does

The pipeline:

- streams the input JSON with `mmap`
- skips repositories that are:
  - forks
  - archived
  - disabled
  - locked
- keeps each candidate repository's `owner/name` and `defaultBranch`
- checks for `.github/workflows` using one of these modes:
  - `graphql`
  - `rest`
  - `head`
  - `none`
- writes one GitHub repository URL per line
- records HTTP/network failures in a separate retry file

## Repository Layout

```text
.
├── filter_github_repositories.py  # entry point
├── requirements.txt       # Python dependencies
└── src/
    ├── cli.py             # CLI arguments and defaults
    ├── main.py            # application entry
    ├── models.py          # data models, progress, stats, error reporting
    ├── parser.py          # streaming repository parsing
    └── pipeline.py        # workflow-checking pipeline
```

## Requirements

- Python 3.14.3
- A repository export downloaded from [SEART GitHub Search](https://seart-ghs.si.usi.ch/)
- Save that downloaded artifact as `input/repositories.json`, or pass its path with `--input`
- Optional: `GITHUB_TOKEN` for faster GitHub API access
- Make sure you installed `uv` in your device

Creating virtual environment:

```bash
uv venv -p 3.14.3 && source .venv/bin/activate
```

Install dependencies:

```bash
uv pip install -r requirements.txt
```

## Input Format

The default input file is:

```text
input/repositories.json
```

This file should be the artifact downloaded from SEART GitHub Search, not an arbitrary GitHub API response dump.

The parser expects a JSON document containing an `items` array. For each item, it reads:

- `name`
- `defaultBranch`
- `isFork`
- `isArchived`
- `isDisabled`
- `isLocked`

If the file does not contain an `items` array, the program exits with an error.

## Usage

Run the default pipeline:

```bash
python filter_github_repositories.py
```

Run with a GitHub token loaded from `.env` or the environment:

```bash
export GITHUB_TOKEN=your_token_here
python filter_github_repositories.py
```

Preview on a limited number of repositories:

```bash
python filter_github_repositories.py --limit 1000
```

Skip workflow checks and output all eligible repositories:

```bash
python filter_github_repositories.py --check none
```

Force a specific check method:

```bash
python filter_github_repositories.py --check graphql
python filter_github_repositories.py --check rest
python filter_github_repositories.py --check head
```

Retry only repositories that previously failed due to HTTP/network errors:

```bash
python filter_github_repositories.py --retry-errors-input output/http_errors.txt
```

## Check Modes

- `auto`: uses `graphql` when `GITHUB_TOKEN` is available, otherwise `rest`
- `graphql`: batched GitHub GraphQL requests
- `rest`: GitHub REST API checks
- `head`: HTTP `HEAD` request against the GitHub web UI
- `none`: no remote check; every eligible repository is written to output

GraphQL mode automatically falls back to `head` mode when the remaining GraphQL rate-limit budget reaches the configured floor.

## Outputs

Default output files:

- `output/repos.txt`: repositories confirmed to match
- `output/http_errors.txt`: repositories that could not be checked due to HTTP/network errors

When retry mode is used:

- `output/repos_retry.txt`
- `output/http_errors_retry.txt`

The error file format is tab-separated:

```text
full_name<TAB>default_branch<TAB>reason
```

## CLI Options

Common options:

- `--input`: path to the source JSON file
- `--output`: path to the matched repository list
- `--error-output`: path to the HTTP/network error file
- `--retry-errors-input`: retry from an existing error file
- `--check`: `auto | graphql | rest | head | none`
- `--token`: GitHub token; defaults to `GITHUB_TOKEN`
- `--workers`: worker count
- `--batch-size`: repositories per request batch
- `--timeout`: per-request timeout in seconds
- `--retries`: retry count for transient failures
- `--limit`: stop after scanning a fixed number of repositories
- `--progress-every`: print progress every N repositories
- `--verbose`: print retry and failure diagnostics

GraphQL-specific options:

- `--graphql-min-remaining`
- `--graphql-limit-action`
- `--graphql-resume-grace-seconds`
- `--graphql-endpoint`

Other endpoint overrides:

- `--rest-endpoint`
- `--head-endpoint`

## Typical Workflow

1. Download the repository artifact from [SEART GitHub Search](https://seart-ghs.si.usi.ch/).
2. Put the downloaded file at `input/repositories.json`, or pass its location with `--input`.
3. Install dependencies.
4. Set `GITHUB_TOKEN` if you want GraphQL or authenticated REST requests.
5. Run `python filter_github_repositories.py`.
6. Read matching repositories from `output/repos.txt`.
7. If needed, retry failures from `output/http_errors.txt`.

## Notes

- Without `GITHUB_TOKEN`, REST mode is heavily rate-limited and may be slow.
- The parser is designed for large input files and does not deserialize the full JSON document.
- Progress and summary logs are written to stderr.
