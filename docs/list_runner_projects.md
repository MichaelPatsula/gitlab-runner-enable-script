# list_runner_projects.py

Lists every project where a given runner is enabled.

```text
list_runner_projects.py --runner X [--group|--project|--membership] [--include-archived] [--verbose]

main():
    args = parse_args()
    client = client_from_args(args)
    runner = resolve_runner(client, X)
    print header to stderr: URL, runner

    for project in iter_projects(client, args):
        if project failed to load: count FAIL, continue
        if archived and not --include-archived: print SKIP to stderr, continue (not counted; only reached with --project)
        enabled = GET /projects/:id/runners (all pages)
            401 -> abort the run
            403 -> count INACCESSIBLE, print to stderr, continue
            other error -> count FAIL, print to stderr, continue
        if runner in enabled:
            count enabled
            stdout: path (or "ENABLED path" with --verbose)
        else:
            count SKIP; print only with --verbose

    print summary to stderr
    exit 1 if any FAIL, else 0
    on Ctrl-C: print partial summary to stderr, exit 130
```

Runner lookup and project iteration are described in [shared steps](shared_steps.md).

## Step: `runner = resolve_runner(client, X)`

This step converts the `--runner` value into a runner record before any projects are scanned.

- `X` is the value passed to `--runner`: either a numeric id such as `6`, or a description such as `old-runner`.
- A numeric value is looked up with `GET /runners/:id`. If that returns 404, the script falls back to a description search.
- A description search lists runners with `GET /runners/all`, or `GET /runners` on 403. It matches the exact description or name first, then case-insensitively.
- The step exits with an error if nothing matches or if more than one runner matches.
- The returned record includes `id`, `runner_type`, `description`, and `paused`. The script uses `id` for every `runner in enabled` check and in the header.

Unlike [enable_runner.py](enable_runner.py), this script does not require a project runner. Group and instance runners are valid here, because the goal is to list every project where the runner is available.

A description lookup on a large instance downloads the full runner list before the header prints, which is the usual cause of the initial pause. Run with `--verbose` to see each lookup step on stderr.
