"""CLI entry point for generating reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .loader import load_scenario
from .planner import plan_migrations
from .reporter import (
    _check_expectations,
    print_report,
    write_html_report,
    write_junit_xml,
    write_markdown_report,
)
from .solver import solve, solve_reachable

# Development fallback: scenarios/ next to the repo root.
# This path does not exist in an installed package; users must pass --scenarios.
_DEV_SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"


def collect_scenarios(base: Path) -> list[Path]:
    return sorted(base.rglob("*.yaml"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ProxLB CP-SAT Solver — run scenarios and generate reports"
    )
    parser.add_argument(
        "--scenarios", type=Path, default=None,
        help=(
            "Directory containing YAML scenario files. "
            "Defaults to scenarios/ relative to the source tree when running "
            "from a checkout; required when running from an installed package."
        ),
    )
    parser.add_argument(
        "--markdown", type=Path, default=None,
        help="Write Markdown report to this path",
    )
    parser.add_argument(
        "--html", type=Path, default=None,
        help="Write HTML report to this path",
    )
    parser.add_argument(
        "--junit", type=Path, default=None,
        help="Write JUnit XML report to this path",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress terminal output",
    )
    args = parser.parse_args()

    if args.scenarios is None:
        if _DEV_SCENARIOS_DIR.is_dir():
            args.scenarios = _DEV_SCENARIOS_DIR
        else:
            parser.error(
                "--scenarios is required when running from an installed package.\n"
                "Example: proxlb-solver --scenarios /path/to/scenarios"
            )

    scenario_files = collect_scenarios(args.scenarios)
    if not scenario_files:
        print("No scenarios found in %s" % args.scenarios, file=sys.stderr)
        sys.exit(1)

    results = []
    results_junit = []
    migration_plans = {}

    for path in scenario_files:
        rel = str(path.relative_to(args.scenarios))
        cluster = load_scenario(path)

        # Use reachable solver (feedback loop)
        solution, mig_plan = solve_reachable(cluster, quiet=args.quiet)

        migration_plans[rel] = mig_plan

        if not args.quiet:
            print_report(cluster, solution)

        results.append((rel, cluster, solution))

        # Evaluate errors for JUnit
        checks = _check_expectations(cluster, solution, mig_plan)
        errors = [
            "%s: %s" % (name, detail)
            for name, _, passed, detail in checks if not passed
        ]
        results_junit.append((rel, cluster, solution, errors))

    if args.markdown:
        write_markdown_report(results, args.markdown, migration_plans)
        if not args.quiet:
            print("\nMarkdown report written to %s" % args.markdown)

    if args.html:
        write_html_report(results, args.html, migration_plans)
        if not args.quiet:
            print("HTML report written to %s" % args.html)

    if args.junit:
        write_junit_xml(results_junit, args.junit)
        if not args.quiet:
            print("JUnit XML written to %s" % args.junit)


if __name__ == "__main__":
    main()
