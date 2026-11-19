# enable_runner.py

Enables one project runner on every project where another project runner is already enabled.

```text
enable_runner.py --if-runner X --enable-runner Y [--apply] [--audit-log PATH [--audit-skips]] [--group|--project|--membership] [--include-archived] [--full-scan] [--verbose]

main():
    args = parse_args()                        # --audit-skips without --audit-log -> usage error
    client = client_from_args(args)            # needs GITLAB_URL and GITLAB_TOKEN
    if_runner = resolve_runner(client, X)      # by id, else by description
    enable_runner = resolve_runner(client, Y)
    validate:
        ids must differ
        both runners must be project runners
        warn if enable_runner is locked
    print header: URL, mode, both runners, scan source
    if --audit-log: open PATH for append (exit if it cannot be opened)

    if --project or --full-scan:
        projects = iter_projects(client, args)              # --project list, --group, or all
    else:
        projects = iter_runner_projects(client, if_runner)  # runner's own projects, filtered

    for project in projects:
        if project failed to load: count FAIL, continue
        if archived and not --include-archived: print SKIP to stderr, audit "skipped" (with --audit-skips), continue (not counted; only reached with --project)
        enabled = GET /projects/:id/runners?type=project_type (all pages)
            401 -> abort the run
            403 -> count INACCESSIBLE, print to stderr, continue
            other error -> count FAIL, print to stderr, continue
        if if_runner not in enabled:                 # reached only with --project or --full-scan, or if the
                                                     # runner was unassigned during the run
            count SKIP; print only with --verbose; audit "skipped" (with --audit-skips)
            continue
        if enable_runner in enabled:
            count ALREADY; print
        elif dry-run:
            print MATCH; audit "planned"
        else:
            POST /projects/:id/runners runner_id=enable_runner
            201 -> count ASSIGN, print ASSIGN, audit "assigned"
            409 -> count ALREADY, print ALREADY
            401 -> abort the run
            other -> count FAIL, print to stderr, audit "failed", continue

    print summary
    if dry-run and matches remain: print "re-run with --apply"
    exit 1 if any FAIL, else 0
    on Ctrl-C: print partial summary (and a re-run hint with --apply), exit 130
    on any other fatal error: print partial summary to stderr, then exit with that error
```

Audit lines are JSONL, flushed and fsynced one at a time. See the README for the record format.

Runner lookup and project iteration are described in [shared steps](shared_steps.md).
