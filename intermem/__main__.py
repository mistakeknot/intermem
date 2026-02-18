"""CLI entry point for intermem synthesis."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from intermem.journal import PromotionJournal
from intermem.metadata import MetadataStore
from intermem.promoter import demote_entries
from intermem.synthesize import run_synthesis
from intermem.validator import sweep_all_entries, validate_promoted


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
    # Shared args available to main parser AND all subcommands.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Project root directory")
    shared.add_argument("--project-root", type=Path, default=None, help="Project root for citation resolution")
    shared.add_argument("--json", action="store_true", help="Output results as JSON")

    parser = argparse.ArgumentParser(description="Intermem memory synthesis", parents=[shared])

    # Synthesis-specific args (stay on main parser for backward compat).
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    parser.add_argument("--auto-approve", action="store_true", help="Skip interactive approval (for testing)")
    parser.add_argument("--validate", action="store_true", help="Validate citations in the synthesis pipeline")
    parser.add_argument("--no-validate", action="store_true", help="Skip citation validation")
    parser.add_argument("--validate-only", action="store_true", help="Standalone mode: validate already-promoted entries")

    # Phase 2A subcommands (optional — no subcommand falls through to synthesis).
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("sweep", parents=[shared], help="Re-validate all entries, apply decay, demote stale")

    query_parser = subparsers.add_parser("query", parents=[shared], help="Query metadata database")
    query_parser.add_argument("--search", type=str, help="Search entries by keyword")
    query_parser.add_argument("--topics", action="store_true", help="List topics with counts")
    query_parser.add_argument("--demoted", action="store_true", help="Show demoted entries")

    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    project_root = args.project_root.resolve() if args.project_root else project_dir
    intermem_dir = project_dir / ".intermem"

    # -- Sweep subcommand --
    if args.command == "sweep":
        try:
            metadata_store = MetadataStore(intermem_dir / "metadata.db")
        except sqlite3.DatabaseError as e:
            print(f"Error: Database error: {e}", file=sys.stderr)
            sys.exit(2)
        journal = PromotionJournal(intermem_dir / "promotion-journal.jsonl")
        sweep_result = sweep_all_entries(metadata_store, project_root, journal)

        if sweep_result.entries_swept == 0:
            print("No entries to sweep.")
            metadata_store.close()
            sys.exit(0)

        demote_result = None
        if sweep_result.demotion_candidates:
            target_docs = _find_target_docs(project_dir)
            demote_result = demote_entries(
                sweep_result.demotion_candidates, metadata_store, target_docs, journal
            )
            metadata_store.conn.commit()

        metadata_store.close()

        if args.json:
            output = {
                "entries_swept": sweep_result.entries_swept,
                "entries_decayed": sweep_result.entries_decayed,
                "entries_marked_for_demotion": sweep_result.entries_marked_for_demotion,
                "demotion_candidates": sweep_result.demotion_candidates,
            }
            if demote_result:
                output["demoted_count"] = demote_result.demoted_count
                output["files_modified"] = demote_result.files_modified
            print(json.dumps(output, indent=2))
        else:
            print(f"Swept {sweep_result.entries_swept} entries.")
            print(f"  Decayed: {sweep_result.entries_decayed}")
            print(f"  Demotion candidates: {len(sweep_result.demotion_candidates)}")
            if demote_result and demote_result.demoted_count > 0:
                print(f"  Demoted: {demote_result.demoted_count} entries from "
                      f"{len(demote_result.files_modified)} files")
        sys.exit(0)

    # -- Query subcommand --
    if args.command == "query":
        db_path = intermem_dir / "metadata.db"
        if not db_path.exists():
            print("Error: metadata.db not found. Run synthesis first.", file=sys.stderr)
            sys.exit(2)
        try:
            metadata_store = MetadataStore(db_path)
        except sqlite3.DatabaseError as e:
            print(f"Error: Database error: {e}", file=sys.stderr)
            sys.exit(2)

        if args.search:
            results = metadata_store.search_entries(args.search)
        elif args.topics:
            results = metadata_store.get_topics()
        elif args.demoted:
            results = metadata_store.get_demoted_entries()
        else:
            metadata_store.close()
            print("Specify --search, --topics, or --demoted", file=sys.stderr)
            sys.exit(1)

        metadata_store.close()

        if args.json:
            print(json.dumps(results, indent=2))
        else:
            if not results:
                print("No results.")
            else:
                for r in results:
                    if args.search:
                        print(f"  [{r.get('status', '?')}] {r.get('content_preview', '')} "
                              f"(confidence: {r.get('confidence', 0):.2f})")
                    elif args.topics:
                        print(f"  {r['section']}: {r['entry_count']} entries "
                              f"(avg confidence: {r.get('avg_confidence', 0):.2f})")
                    elif args.demoted:
                        print(f"  {r.get('content_preview', '')} "
                              f"(demoted: {r.get('demoted_at', 'unknown')})")
        sys.exit(0)

    # -- Default: synthesis / validate-only (backward compatible) --
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
