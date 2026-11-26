#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip pre-commit
pre-commit install
pre-commit install-hooks

printf 'Development setup complete. Activate the venv in a new shell with: source .venv/bin/activate\n'
