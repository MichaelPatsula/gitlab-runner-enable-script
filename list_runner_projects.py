#!/usr/bin/env python3
"""List GitLab projects where a specific runner is enabled.

A runner is enabled on a project when it appears in that project's runner list:
an assigned project runner, an allowed group runner, or an allowed instance
runner. Matching project paths are printed one per line. Progress and errors
go to stderr. Pass --verbose to also print projects where the runner
is not enabled.

Requires Python 3.9+ and a GitLab token with the api scope.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from gitlab_api import (
    GitLabError,
    RunnerScan,
    add_scan_arguments,
    client_from_args,
    exit_gitlab_error,
    exit_interrupted,
    iter_projects,
    iter_projects_with_runner,
    project_label,
    resolve_runner,
    runner_label,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List GitLab projects where a specific runner is enabled.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  export GITLAB_URL=https://gitlab.example.com
  export GITLAB_TOKEN=glpat-...

  %(prog)s --runner 6
  %(prog)s --runner old-runner --group my-group/sub-group
  %(prog)s --runner 6 --verbose
""",
    )
    add_scan_arguments(parser)
    parser.add_argument(
        "--runner",
        required=True,
        help="Runner id or description to look for.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print projects where the runner is not enabled, and log runner lookup steps to stderr.",
    )
    return parser


def _summary(stats: RunnerScan) -> str:
    return (
        f"Scanned {stats.scanned} projects; runner enabled on {stats.enabled}; "
        f"skipped {stats.skipped}; inaccessible {stats.inaccessible}; failed {stats.failed}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = client_from_args(args)
        runner = resolve_runner(client, args.runner)
        print(f"GitLab: {client.base_url}", file=sys.stderr)
        print(f"Runner: {runner_label(runner)}", file=sys.stderr)

        stats = RunnerScan()
        try:
            for project, _enabled_ids in iter_projects_with_runner(
                client,
                int(runner["id"]),
                iter_projects(client, args),
                args.include_archived,
                stats,
                args.verbose,
            ):
                if args.verbose:
                    print(f"ENABLED {project_label(project)}")
                else:
                    path = project.get("path_with_namespace") or project.get("path") or str(project["id"])
                    print(path)
        except KeyboardInterrupt:
            exit_interrupted(_summary(stats))

        print(_summary(stats), file=sys.stderr)
        return 1 if stats.failed else 0
    except KeyboardInterrupt:
        exit_interrupted()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GitLabError as error:
        exit_gitlab_error(error)
    except KeyboardInterrupt:
        exit_interrupted()
