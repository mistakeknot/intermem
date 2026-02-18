"""CLI entry point for intermem synthesis."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from intermem.synthesize import run_synthesis
from intermem.metadata import MetadataStore
from intermem.validator import validate_promoted


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
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate citations in the synthesis pipeline (or standalone with --validate-only)",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip citation validation",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Standalone mode: validate already-promoted entries and report",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root for citation resolution (default: auto-detect)",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    project_root = args.project_root.resolve() if args.project_root else project_dir

    target_docs = _find_target_docs(project_dir)

    # Standalone validate mode.
    if args.validate_only:
        if not target_docs:
            print(f"No AGENTS.md or CLAUDE.md found in {project_dir}")
            sys.exit(1)
        intermem_dir = project_dir / ".intermem"
        metadata_store = MetadataStore(intermem_dir / "metadata.db")
        vresult = validate_promoted(target_docs, metadata_store, project_root)
        metadata_store.close()

        if args.json:
            reports = [
                {
                    "content_preview": r.content_preview,
                    "section": r.section,
                    "citations_found": r.citations_found,
                    "citations_valid": r.citations_valid,
                    "citations_broken": r.citations_broken,
                    "confidence": r.confidence,
                    "broken_details": r.broken_details,
                }
                for r in vresult.reports
            ]
            print(json.dumps({
                "entries_checked": vresult.entries_checked,
                "entries_healthy": vresult.entries_healthy,
                "entries_stale": vresult.entries_stale,
                "reports": reports,
            }, indent=2))
        else:
            print(f"Validated {vresult.entries_checked} promoted entries.")
            print(f"  Healthy: {vresult.entries_healthy}  Stale: {vresult.entries_stale}")
            for report in vresult.reports:
                status = "STALE" if report.confidence < 0.3 else "OK"
                print(f"  [{status}] {report.content_preview[:60]}... "
                      f"(confidence: {report.confidence:.2f}, "
                      f"citations: {report.citations_valid}v/{report.citations_broken}b)")
                for detail in report.broken_details:
                    print(f"         {detail}")
        return

    # Normal synthesis mode.
    memory_dir = _find_memory_dir(project_dir)
    if memory_dir is None:
        print(f"No auto-memory directory found for {project_dir}")
        print("Expected at: ~/.claude/projects/<encoded-path>/memory/")
        sys.exit(1)

    if not target_docs:
        print(f"No AGENTS.md or CLAUDE.md found in {project_dir}")
        print("At least one target document is needed for promotion.")
        sys.exit(1)

    intermem_dir = project_dir / ".intermem"
    do_validate = args.validate and not args.no_validate

    result = run_synthesis(
        memory_dir=memory_dir,
        target_docs=target_docs,
        intermem_dir=intermem_dir,
        auto_approve=args.auto_approve,
        dry_run=args.dry_run,
        validate=do_validate,
        project_root=project_root if do_validate else None,
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
                    "validated_count": result.validated_count,
                    "stale_filtered": result.stale_filtered,
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
        if result.stale_filtered:
            print(f"  Filtered {result.stale_filtered} stale entries (broken citations).")
        print(f"Auto-memory: {result.new_line_count} lines remaining.")
        return

    if result.candidates_count > 0:
        msg = f"Found {result.candidates_count} candidates (skipped {result.duplicates_skipped} duplicates)."
        if result.stale_filtered:
            msg += f" Filtered {result.stale_filtered} stale."
        print(msg)
        return

    print(
        f"Scanned {result.total_entries} entries. {result.stable_entries} stable, none ready for promotion."
    )
    if result.stale_filtered:
        print(f"  {result.stale_filtered} entries filtered as stale (broken citations).")
    if result.stable_entries == 0:
        print("No entries stable across 3+ snapshots yet. Keep running periodically.")


if __name__ == "__main__":
    main()
