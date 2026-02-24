"""CLI entry point for generating reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .loader import load_scenario
from .planner import plan_migrations
from .reporter import (
    print_report, write_html_report, write_junit_xml, write_markdown_report,
)
from .solver import solve

SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"


def collect_scenarios(base: Path) -> list[Path]:
    return sorted(base.rglob("*.yaml"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ProxLB CP-SAT Solver — run scenarios and generate reports"
    )
    parser.add_argument(
        "--scenarios", type=Path, default=SCENARIOS_DIR,
        help="Directory containing YAML scenarios",
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

    scenario_files = collect_scenarios(args.scenarios)
    if not scenario_files:
        print("No scenarios found in %s" % args.scenarios, file=sys.stderr)
        sys.exit(1)

    results_md = []
    results_junit = []
    migration_plans = {}

    for path in scenario_files:
        rel = str(path.relative_to(args.scenarios))
        cluster = load_scenario(path)
        solution = solve(cluster)

        # Plan migration execution order
        mig_plan = plan_migrations(cluster, solution)
        migration_plans[rel] = mig_plan

        if not args.quiet:
            print_report(cluster, solution)

        results_md.append((rel, cluster, solution))

        # Evaluate errors for JUnit
        from .reporter import _check_expectations
        checks = _check_expectations(cluster, solution)
        errors = [
            "%s: %s" % (name, detail)
            for name, _, passed, detail in checks if not passed
        ]
        results_junit.append((rel, cluster, solution, errors))

    if args.markdown:
        write_markdown_report(results_md, args.markdown, migration_plans)
        if not args.quiet:
            print("\nMarkdown report written to %s" % args.markdown)

    if args.html:
        write_html_report(results_md, args.html, migration_plans)
        if not args.quiet:
            print("HTML report written to %s" % args.html)

    if args.junit:
        write_junit_xml(results_junit, args.junit)
        if not args.quiet:
            print("JUnit XML written to %s" % args.junit)


if __name__ == "__main__":
    main()
