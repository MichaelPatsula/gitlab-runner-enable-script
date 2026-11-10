#!/usr/bin/env python3
"""Enable one GitLab runner on every project where another runner is already enabled.

The scan of projects comes from gitlab_api.py. Both runners must be project runners.
A group or instance runner is inherited by many projects, so it cannot be used
as --if-runner. Nothing is changed unless --apply is passed.

Requires Python 3.9+ and a GitLab token with the api scope. Assigning a runner
requires the Maintainer or Owner role on the project that owns the runner and
on the target project, or a custom role with admin_runners.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from gitlab_api import (
    GitLabClient,
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


@dataclass
class Stats:
    scanned: int = 0
    matched: int = 0
    assigned: int = 0
    already: int = 0
    skipped: int = 0
    failed: int = 0
    inaccessible: int = 0


def enable_on_projects(
    client: GitLabClient,
    projects: Iterable[Mapping[str, Any]],
    if_runner_id: int,
    enable_runner_id: int,
    apply: bool,
    include_archived: bool,
    verbose: bool,
    stats: Optional[Stats] = None,
) -> Stats:
    if stats is None:
        stats = Stats()
    scan = RunnerScan()
    try:
        for project, enabled_ids in iter_projects_with_runner(
            client,
            if_runner_id,
            projects,
            include_archived,
            scan,
            verbose,
        ):
            label = project_label(project)
            if enable_runner_id in enabled_ids:
                stats.already += 1
                print(f"ALREADY {label}: runner #{enable_runner_id} is already enabled")
                continue
            if not apply:
                print(f"MATCH {label}: runner #{if_runner_id} is enabled; would enable runner #{enable_runner_id}")
                continue
            _assign(client, int(project["id"]), enable_runner_id, label, stats)
    finally:
        stats.scanned = scan.scanned
        stats.matched = scan.enabled
        stats.skipped = scan.skipped
        stats.failed += scan.failed
        stats.inaccessible = scan.inaccessible
    return stats


def _assign(client: GitLabClient, project_id: int, enable_runner_id: int, label: str, stats: Stats) -> None:
    try:
        client.assign_runner(project_id, enable_runner_id)
    except GitLabError as exc:
        if exc.status == 401:
            raise
        if exc.status == 409:
            stats.already += 1
            print(f"ALREADY {label}: runner #{enable_runner_id} is already enabled")
            return
        stats.failed += 1
        print(f"FAIL {label}: {exc.message}", file=sys.stderr)
        return
    stats.assigned += 1
    print(f"ASSIGN {label}: enabled runner #{enable_runner_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="On each GitLab project where one runner is enabled, enable another runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  export GITLAB_URL=https://gitlab.example.com
  export GITLAB_TOKEN=glpat-...

  # Preview projects that have runner 6 and would gain runner 9
  %(prog)s --if-runner 6 --enable-runner 9

  # Enable runner 9 on those projects
  %(prog)s --if-runner 6 --enable-runner 9 --apply

  # Limit the scan to a group, matching runners by description
  %(prog)s --group my-group/sub-group --if-runner old-runner --enable-runner new-runner --apply
""",
    )
    add_scan_arguments(parser)
    parser.add_argument(
        "--if-runner",
        required=True,
        help="Project runner id or description that must already be assigned to the project.",
    )
    parser.add_argument(
        "--enable-runner",
        required=True,
        help="Project runner id or description to enable on those projects.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Enable that runner. Without this flag the script only reports matches.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print projects where --if-runner is not enabled, and log runner lookup steps to stderr.",
    )
    return parser


def _summary(stats: Stats) -> str:
    return (
        "Scanned {scanned} projects; runner enabled on {matched}; "
        "assigned {assigned}; already enabled {already}; skipped {skipped}; "
        "inaccessible {inaccessible}; failed {failed}".format(
            scanned=stats.scanned,
            matched=stats.matched,
            assigned=stats.assigned,
            already=stats.already,
            skipped=stats.skipped,
            inaccessible=stats.inaccessible,
            failed=stats.failed,
        )
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = client_from_args(args)
        if_runner = resolve_runner(client, args.if_runner)
        enable_runner = resolve_runner(client, args.enable_runner)
        if_runner_id = int(if_runner["id"])
        enable_runner_id = int(enable_runner["id"])
        if if_runner_id == enable_runner_id:
            raise SystemExit("--if-runner and --enable-runner are the same runner.")
        if if_runner.get("runner_type") != "project_type":
            raise SystemExit(
                f"{runner_label(if_runner)} is not a project runner. "
                "Group and instance runners are inherited by many projects. "
                f"Run list_runner_projects.py --runner {if_runner_id} to list every project where it is available."
            )
        if enable_runner.get("runner_type") != "project_type":
            raise SystemExit(
                f"Runner to enable {runner_label(enable_runner)} is not a project runner. "
                "GitLab can assign only project runners to a project."
            )
        if enable_runner.get("locked") is True:
            print(
                f"Warning: runner to enable {runner_label(enable_runner)} is locked. "
                "Assignment fails unless the token belongs to an administrator.",
                file=sys.stderr,
            )

        print(f"GitLab: {client.base_url}")
        print(f"Mode: {'apply' if args.apply else 'dry-run'}")
        print(f"If runner: {runner_label(if_runner)}")
        print(f"Enable runner: {runner_label(enable_runner)}")

        stats = Stats()
        try:
            enable_on_projects(
                client,
                iter_projects(client, args),
                if_runner_id,
                enable_runner_id,
                args.apply,
                args.include_archived,
                args.verbose,
                stats,
            )
        except KeyboardInterrupt:
            extra = (
                "Assigns already completed stay applied. Re-run to finish."
                if args.apply
                else ""
            )
            exit_interrupted(_summary(stats), extra)

        print(_summary(stats))
        if not args.apply and stats.matched > stats.already:
            print("Dry-run only. Re-run with --apply to enable the runner.")
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
