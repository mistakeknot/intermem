#!/usr/bin/env bash
set -euo pipefail
MONOREPO="$(cd "$(dirname "$0")/../.." && pwd)"
HARNESS="${INTERLAB_HARNESS:-$MONOREPO/interverse/interlab/scripts/py-bench-harness.sh}"
DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$HARNESS" --cmd "uv run pytest tests/ -q --tb=no" --metric test_pass_rate --dir "$DIR" --mode pytest
