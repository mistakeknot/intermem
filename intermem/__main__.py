"""CLI entry point for intermem synthesis."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from intermem.synthesize import run_synthesis


def _find_memory_dir(project_dir: Path) -> Path | None:
    """Find the auto-memory directory for a project."""
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None

    encoded = str(project_dir).replace("/", "-")
    if not encoded.startswith("-"):
        encoded = "-" + encoded

    memory_dir = claude_projects / encoded / "memory"
    if memory_dir.exists():
        return memory_dir
    return None


def _find_target_docs(project_dir: Path) -> list[Path]:
    """Find AGENTS.md and CLAUDE.md in the project directory."""
    docs: list[Path] = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = project_dir / name
        if path.exists():
            docs.append(path)
    return docs


def main() -> None:
    parser = argparse.ArgumentParser(description="Intermem memory synthesis")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Project root directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying files",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip interactive approval (for testing)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    memory_dir = _find_memory_dir(project_dir)
    if memory_dir is None:
        print(f"No auto-memory directory found for {project_dir}")
        print("Expected at: ~/.claude/projects/<encoded-path>/memory/")
        sys.exit(1)

    target_docs = _find_target_docs(project_dir)
    if not target_docs:
        print(f"No AGENTS.md or CLAUDE.md found in {project_dir}")
        print("At least one target document is needed for promotion.")
        sys.exit(1)

    intermem_dir = project_dir / ".intermem"

    result = run_synthesis(
        memory_dir=memory_dir,
        target_docs=target_docs,
        intermem_dir=intermem_dir,
        auto_approve=args.auto_approve,
        dry_run=args.dry_run,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "total_entries": result.total_entries,
                    "stable_entries": result.stable_entries,
                    "duplicates_skipped": result.duplicates_skipped,
                    "candidates_count": result.candidates_count,
                    "promoted_count": result.promoted_count,
                    "pruned_count": result.pruned_count,
                    "baseline_recorded": result.baseline_recorded,
                    "new_line_count": result.new_line_count,
                },
                indent=2,
            )
        )
        return

    if result.baseline_recorded:
        print(f"Building baseline - recorded {result.total_entries} entries.")
        print("Run again after your next few sessions to identify stable facts.")
        return

    if result.promoted_count > 0:
        print(
            f"Promoted {result.promoted_count} entries, pruned {result.pruned_count} lines."
        )
        print(f"Auto-memory: {result.new_line_count} lines remaining.")
        return

    if result.candidates_count > 0:
        print(
            f"Found {result.candidates_count} candidates (skipped {result.duplicates_skipped} duplicates)."
        )
        return

    print(
        f"Scanned {result.total_entries} entries. {result.stable_entries} stable, none ready for promotion."
    )
    if result.stable_entries == 0:
        print("No entries stable across 3+ snapshots yet. Keep running periodically.")


if __name__ == "__main__":
    main()
