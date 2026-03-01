#!/usr/bin/env bash
set -euo pipefail
# intermem session-start hook — source interbase and nudge companions
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$HOOK_DIR/interbase-stub.sh"

ib_session_status
ib_nudge_companion "interwatch" "Enables citation freshness checking for promoted entries"

# --- MEMORY.md line budget nudge ---
_intermem_budget=120

# Find memory dir for current project
_intermem_project_dir="${PWD}"
_intermem_encoded="${_intermem_project_dir//\//-}"
_intermem_memory_dir="${HOME}/.claude/projects/${_intermem_encoded}/memory"

if [[ -f "${_intermem_memory_dir}/MEMORY.md" ]]; then
    _intermem_lines=$(wc -l < "${_intermem_memory_dir}/MEMORY.md" 2>/dev/null) || _intermem_lines=0
    if [[ "${_intermem_lines}" -gt "${_intermem_budget}" ]]; then
        echo "intermem: MEMORY.md is ${_intermem_lines} lines (budget: ${_intermem_budget}). Run /intermem:tidy to review."
    fi
fi
