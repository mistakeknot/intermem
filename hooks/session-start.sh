#!/usr/bin/env bash
set -euo pipefail
# intermem session-start hook — source interbase and nudge companions
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$HOOK_DIR/interbase-stub.sh"

ib_session_status
ib_nudge_companion "interwatch" "Enables citation freshness checking for promoted entries"
