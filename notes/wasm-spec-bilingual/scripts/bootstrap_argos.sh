#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"
python_bin="${PYTHON_BIN:-python3.10}"

"$python_bin" -m venv "$project_dir/.venv"
"$project_dir/.venv/bin/python" -m pip install --upgrade pip
"$project_dir/.venv/bin/python" -m pip install -r "$project_dir/requirements.txt"
"$project_dir/.venv/bin/python" -m pip install argostranslate==1.11.0 --no-deps

printf 'Argos environment ready: %s\n' "$project_dir/.venv/bin/python"
