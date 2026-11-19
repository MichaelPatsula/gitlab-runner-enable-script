#!/usr/bin/env python3
"""Enable one GitLab runner on every project where another runner is already enabled.

Candidate projects come from --if-runner's own project list, unless --project or
--full-scan is given. Both runners must be project runners.
A group or instance runner is inherited by many projects, so it cannot be used
as --if-runner. Nothing is changed unless --apply is passed.

Requires Python 3.9+ and a GitLab token with the api scope. Assigning a runner
requires the Maintainer or Owner role on the project that owns the runner and
on the target project, or a custom role with admin_runners.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, TextIO

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
    iter_runner_projects,
    project_label,
    resolve_runner,
    runner_label,
    uses_runner_projects,
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


class AuditLog:
    """Append-only JSONL record of planned, applied, and failed runner assignments.

    Each line is flushed and fsynced as it is written, so a crash or Ctrl-C mid-run
    leaves every earlier line intact.
    """

    def __init__(
        self,
        file: TextIO,
        gitlab: str,
        if_runner_id: int,
        enable_runner_id: int,
        include_skips: bool = False,
    ) -> None:
        self._file = file
        self._base = {"gitlab": gitlab, "if_runner_id": if_runner_id, "enable_runner_id": enable_runner_id}
        self.include_skips = include_skips

    def record(self, event: str, project: Mapping[str, Any], **extra: Any) -> None:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **extra,
            "gitlab": self._base["gitlab"],
            "project_id": project.get("id"),
            "project_path": project.get("path_with_namespace") or project.get("path"),
            "if_runner_id": self._base["if_runner_id"],
            "enable_runner_id": self._base["enable_runner_id"],
        }
        self._file.write(json.dumps(entry) + "\n")
        self._file.flush()
        try:
            fd = self._file.fileno()
        except (AttributeError, io.UnsupportedOperation):
            return  # in-memory stream, nothing to sync
        os.fsync(fd)


def enable_on_projects(
    client: GitLabClient,
    projects: Iterable[Mapping[str, Any]],
    if_runner_id: int,
    enable_runner_id: int,
    apply: bool,
    include_archived: bool,
    verbose: bool,
    stats: Optional[Stats] = None,
    audit: Optional[AuditLog] = None,
) -> Stats:
    if stats is None:
        stats = Stats()
    scan = RunnerScan()
    on_skip = None
    if audit is not None and audit.include_skips:

        def on_skip(project: Mapping[str, Any], reason: str) -> None:
            audit.record("skipped", project, reason=reason)

    try:
        for project, enabled_ids in iter_projects_with_runner(
            client,
            if_runner_id,
            projects,
            include_archived,
            scan,
            verbose,
            on_skip,
            runner_type="project_type",
        ):
            label = project_label(project)
            if enable_runner_id in enabled_ids:
                stats.already += 1
                print(f"ALREADY {label}: runner #{enable_runner_id} is already enabled")
                continue
            if not apply:
                print(f"MATCH {label}: runner #{if_runner_id} is enabled; would enable runner #{enable_runner_id}")
                if audit is not None:
                    audit.record("planned", project)
                continue
            _assign(client, project, enable_runner_id, label, stats, audit)
    finally:
        stats.scanned = scan.scanned
        stats.matched = scan.enabled
        stats.skipped = scan.skipped
        stats.failed += scan.failed
        stats.inaccessible = scan.inaccessible
    return stats


def _assign(
    client: GitLabClient,
    project: Mapping[str, Any],
    enable_runner_id: int,
    label: str,
    stats: Stats,
    audit: Optional[AuditLog] = None,
) -> None:
    try:
        client.assign_runner(int(project["id"]), enable_runner_id)
    except GitLabError as exc:
        if exc.status == 401:
            raise
        if exc.status == 409:
            stats.already += 1
            print(f"ALREADY {label}: runner #{enable_runner_id} is already enabled")
            return
        stats.failed += 1
        print(f"FAIL {label}: {exc.message}", file=sys.stderr)
        if audit is not None:
            audit.record("failed", project, status=exc.status, error=exc.message)
        return
    stats.assigned += 1
    print(f"ASSIGN {label}: enabled runner #{enable_runner_id}")
    if audit is not None:
        audit.record("assigned", project)


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

  # Keep a JSONL record of every assignment (append-only, safe to reuse across runs)
  %(prog)s --if-runner 6 --enable-runner 9 --apply --audit-log runner-changes.jsonl
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
        "--audit-log",
        metavar="PATH",
        help=(
            "Append one JSON line per planned (dry-run), assigned, or failed assignment to PATH. "
            "The file is created if missing and never truncated."
        ),
    )
    parser.add_argument(
        "--audit-skips",
        action="store_true",
        help="Also record skipped projects (archived, or --if-runner not enabled) in --audit-log. "
        "Projects without --if-runner are only visited with --full-scan or --project.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print projects where --if-runner is not enabled (with --full-scan or --project), "
        "and log runner lookup steps to stderr.",
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
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.audit_skips and not args.audit_log:
        parser.error("--audit-skips requires --audit-log")
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
        from_runner = uses_runner_projects(args, if_runner)
        if from_runner:
            print(f"Scan: projects assigned to runner #{if_runner_id} (pass --full-scan to check every project)")
        else:
            print("Scan: named projects" if args.project else "Scan: every project")

        audit_file: Optional[TextIO] = None
        audit: Optional[AuditLog] = None
        if args.audit_log:
            try:
                audit_file = open(args.audit_log, "a", encoding="utf-8")
            except OSError as exc:
                raise SystemExit(f"Cannot open audit log {args.audit_log}: {exc.strerror}")
            audit = AuditLog(audit_file, client.base_url, if_runner_id, enable_runner_id, args.audit_skips)
            print(f"Audit log: {args.audit_log}")

        stats = Stats()
        projects = iter_runner_projects(client, if_runner_id, args) if from_runner else iter_projects(client, args)
        try:
            enable_on_projects(
                client,
                projects,
                if_runner_id,
                enable_runner_id,
                args.apply,
                args.include_archived,
                args.verbose,
                stats,
                audit,
            )
        except KeyboardInterrupt:
            extra = (
                "Assigns already completed stay applied. Re-run to finish."
                if args.apply
                else ""
            )
            exit_interrupted(_summary(stats), extra)
        except BaseException:
            print(_summary(stats), file=sys.stderr)
            raise
        finally:
            if audit_file is not None:
                audit_file.close()

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
