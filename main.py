#!/usr/bin/env python3
"""
Jenkins-Jira Automation — CLI Entry Point.

Automatically creates Jira tickets when Jenkins pipeline jobs fail.
Detects the first failing stage, extracts relevant links, and assigns
the ticket to the appropriate POC based on configuration.

Usage:
    # Process a specific build
    python main.py --category sandbox --job ngkmd_410_game_custom_test --build 123

    # Process latest failed build for a job
    python main.py --category sandbox --job ngkmd_410_game_custom_test

    # Process all jobs in a category (latest failures)
    python main.py --category sandbox

    # Scan all categories and jobs
    python main.py --scan-all

    # Dry run (preview without creating tickets)
    python main.py --dry-run --category sandbox --job ngkmd_410_game_custom_test --build 123

    # Verbose logging
    python main.py -v --category sandbox --job X --build 123

    # JSON output
    python main.py --json --category sandbox --job X --build 123
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

# Ensure the project root is in the Python path
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.config_loader import ConfigError, load_settings
from src.orchestrator import AutomationError, Orchestrator


def setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Configure logging with appropriate level and format."""
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)-25s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="jenkins-jira-automation",
        description=(
            "Automatically create Jira tickets for failed Jenkins pipeline jobs. "
            "Detects the first failing stage, extracts links, and assigns to the "
            "correct POC based on YAML configuration."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --category sandbox --job my_test --build 123
  %(prog)s --category sandbox --job my_test          # latest failed build
  %(prog)s --category sandbox                         # all jobs in category
  %(prog)s --scan-all                                 # all categories
  %(prog)s --dry-run --category sandbox --job X --build 42

Environment Variables:
  JENKINS_URL        Jenkins base URL
  JENKINS_USER       Jenkins username
  JENKINS_API_TOKEN  Jenkins API token
  JIRA_URL           Jira base URL
  JIRA_USER          Jira username/email
  JIRA_API_TOKEN     Jira API token
  CONFIG_DIR         Override config directory path
        """,
    )

    # Mode of operation
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--scan-all",
        action="store_true",
        help="Scan all categories and jobs for latest failures",
    )

    # Target selection
    parser.add_argument(
        "--category", "-c",
        type=str,
        help="Category name (e.g., sandbox, kmdx, stc)",
    )
    parser.add_argument(
        "--job", "-j",
        type=str,
        help="Job name within the category",
    )
    parser.add_argument(
        "--build", "-b",
        type=str,
        default=None,
        help="Build number, 'latest', or 'lastFailed' (default: lastFailed)",
    )

    # Behavior flags
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview ticket without creating it in Jira",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON (useful for piping)",
    )

    # Logging
    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    log_group.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress informational output",
    )

    # Validation
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate configuration files and exit",
    )

    return parser


def validate_config() -> bool:
    """Validate all configuration files and report results."""
    from src.config_loader import load_all_categories, load_settings

    print("Validating configuration...")
    errors = []

    try:
        settings = load_settings()
        print(f"  ✅ settings.yaml loaded successfully")
    except ConfigError as e:
        errors.append(f"  ❌ settings.yaml: {e}")
        print(errors[-1])

    try:
        categories = load_all_categories()
        for cat_name, cat_data in categories.items():
            jobs = cat_data.get("jobs", {}) or {}
            print(f"  ✅ {cat_name}.yaml: {len(jobs)} jobs defined")

            # Validate each job
            for job_name in jobs:
                try:
                    from src.config_loader import resolve_job_config
                    job_config = resolve_job_config(cat_data, job_name)
                    print(
                        f"      ✅ {job_name}: {len(job_config.all_stages)} stages, "
                        f"ramdump={job_config.ramdump_required}"
                    )
                except ConfigError as e:
                    errors.append(f"      ❌ {job_name}: {e}")
                    print(errors[-1])
    except ConfigError as e:
        errors.append(f"  ❌ Categories: {e}")
        print(errors[-1])

    if errors:
        print(f"\n❌ Validation failed with {len(errors)} error(s)")
        return False
    else:
        print(f"\n✅ All configuration files are valid")
        return True


def format_json_result(result) -> str:
    """Format a result as JSON."""
    if result is None:
        return json.dumps({"status": "skipped", "reason": "Build not failed"})
    if hasattr(result, "__dataclass_fields__"):
        return json.dumps(asdict(result), indent=2, default=str)
    if isinstance(result, dict):
        # For scan-all results
        output = {}
        for cat, tickets in result.items():
            output[cat] = [asdict(t) for t in tickets] if tickets else []
        return json.dumps(output, indent=2, default=str)
    if isinstance(result, list):
        return json.dumps(
            [asdict(t) for t in result], indent=2, default=str
        )
    return json.dumps({"result": str(result)})


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(verbose=args.verbose, quiet=args.quiet)
    logger = logging.getLogger("main")

    # Validate config mode
    if args.validate_config:
        return 0 if validate_config() else 1

    # Validate arguments
    if not args.scan_all and not args.category:
        parser.error(
            "Either --scan-all or --category is required. "
            "Use --help for usage."
        )

    if args.build and not args.job:
        parser.error("--build requires --job to be specified.")

    # Run orchestrator
    exit_code = 0
    try:
        with Orchestrator(dry_run=args.dry_run) as orchestrator:
            if args.scan_all:
                # ── Scan all categories ─────────────────────────────────
                logger.info("Scanning all categories for failures...")
                results = orchestrator.process_all()

                if args.json_output:
                    print(format_json_result(results))
                else:
                    total = sum(len(v) for v in results.values())
                    print(f"\n📊 Scan complete: {total} ticket(s) across "
                          f"{len(results)} category(ies)")
                    for cat, tickets in results.items():
                        if tickets:
                            for t in tickets:
                                status = "🔍 Preview" if args.dry_run else "✅ Created"
                                print(f"  {status}: {t.issue_key or 'N/A'} — {t.summary}")

            elif args.category and args.job and args.build:
                # ── Specific build ──────────────────────────────────────
                result = orchestrator.process_build(
                    args.category, args.job, args.build
                )
                if args.json_output:
                    print(format_json_result(result))
                elif result and not args.dry_run:
                    print(f"\n✅ Ticket created: {result.issue_key} ({result.issue_url})")
                elif result is None:
                    print("\nℹ️  Build is not failed — no ticket created.")

            elif args.category and args.job:
                # ── Latest failed build for a job ───────────────────────
                result = orchestrator.process_job_latest(args.category, args.job)
                if args.json_output:
                    print(format_json_result(result))
                elif result and not args.dry_run:
                    print(f"\n✅ Ticket created: {result.issue_key} ({result.issue_url})")
                elif result is None:
                    print(f"\nℹ️  No failed builds found for {args.job}.")

            elif args.category:
                # ── All jobs in a category ──────────────────────────────
                results = orchestrator.process_category(args.category)
                if args.json_output:
                    print(format_json_result(results))
                else:
                    print(f"\n📊 Category '{args.category}': "
                          f"{len(results)} ticket(s) created")
                    for t in results:
                        status = "🔍 Preview" if args.dry_run else "✅ Created"
                        print(f"  {status}: {t.issue_key or 'N/A'} — {t.summary}")

    except AutomationError as e:
        logger.error(f"Automation error: {e}")
        if args.json_output:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"\n❌ Error: {e}", file=sys.stderr)
        exit_code = 1

    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        if args.json_output:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"\n❌ Configuration error: {e}", file=sys.stderr)
            print("Run with --validate-config to check your configuration.", file=sys.stderr)
        exit_code = 1

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user.", file=sys.stderr)
        exit_code = 130

    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        if args.json_output:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"\n❌ Unexpected error: {e}", file=sys.stderr)
        exit_code = 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
