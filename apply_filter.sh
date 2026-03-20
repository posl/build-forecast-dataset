#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

if [[ -f .venv/bin/activate ]]; then
  # Activate the local venv so python-dotenv and other local deps are available.
  source .venv/bin/activate
fi

python "$script_dir/apply_filter.py" "$@"
