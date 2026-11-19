# GitLab runner scripts

Python scripts that scan GitLab projects and check which runners are enabled. They use the GitLab REST API and the Python standard library (Python 3.9+). No extra packages are required.

`gitlab_api.py` is the shared client and project scan. `list_runner_projects.py` and `enable_runner.py` are the commands.

A runner is enabled on a project when it appears in that project's runner list. That includes an assigned project runner, an allowed group runner, or an allowed instance runner.

## Setup

Create a personal, project, or group access token with the `api` scope. Assigning a runner also requires the Maintainer or Owner role on the project that owns the runner and on the target project, or a custom role with `admin_runners`.

```bash
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=glpat-...
```

Pass the same values as `--gitlab-url` and `--token` if you prefer not to use the environment. A value that is all digits is treated as a runner id. Any other value is matched against the runner description.

## List projects for a runner

`list_runner_projects.py` prints each matching project path on its own line. Progress and the summary go to stderr. Pass `--verbose` to also print projects where the runner is not enabled.

```bash
python3 list_runner_projects.py --runner 6
python3 list_runner_projects.py --runner old-runner --group my-group/sub-group
python3 list_runner_projects.py --runner 6 --verbose
```

## Enable a runner where another runner is already enabled

`enable_runner.py` finds projects where `--if-runner` is assigned and enables `--enable-runner` on those projects. Both runners must be project runners. GitLab can assign only project runners onto additional projects. A group or instance runner is inherited by many projects; use `list_runner_projects.py` to see where one of those is available.

The script only reports matches until you pass `--apply`. Dry-run lines mean:

- `MATCH` — `--if-runner` is enabled and `--enable-runner` is not. `--apply` would assign it here.
- `ALREADY` — both runners are already enabled.
- `SKIP` — `--if-runner` is not enabled. Printed only with `--verbose`; `--enable-runner` is not considered.

```bash
# Preview
python3 enable_runner.py --if-runner 6 --enable-runner 9

# Preview, including projects that do not have runner 6
python3 enable_runner.py --if-runner 6 --enable-runner 9 --verbose

# Enable runner 9 on each match
python3 enable_runner.py --if-runner 6 --enable-runner 9 --apply

# Match runners by description, limited to a group
python3 enable_runner.py --group my-group/sub-group \
  --if-runner old-runner --enable-runner new-runner --apply
```

### Audit log

Pass `--audit-log PATH` to keep a record of what the script planned or changed. It works with and without `--apply`. Each event appends one JSON object per line (JSONL). The file is never truncated, so several runs can share one file. Each line is flushed to disk before the script moves on, so a crash or Ctrl+C keeps every earlier line.

| `event` | When | Extra fields |
| --- | --- | --- |
| `planned` | Dry-run match: `--apply` would assign the runner. | |
| `assigned` | `--apply` assigned the runner. | |
| `failed` | `--apply` tried to assign and GitLab returned an error. | `status`, `error` |
| `skipped` | Only with `--audit-skips`: the project is archived, or `--if-runner` is not enabled. | `reason` (`archived` or `runner_not_enabled`) |

Projects that already have `--enable-runner` are not recorded, because nothing changed. Every line also has `time` (UTC), `gitlab`, `project_id`, `project_path`, `if_runner_id`, and `enable_runner_id`.

```json
{"time": "2026-11-18T23:15:04+00:00", "event": "assigned", "gitlab": "https://gitlab.example.com", "project_id": 1234, "project_path": "platform/payments-api", "if_runner_id": 6, "enable_runner_id": 9}
```

```bash
python3 enable_runner.py --if-runner 6 --enable-runner 9 --apply --audit-log runner-changes.jsonl

# Projects that gained runner 9 from the script
jq -r 'select(.event == "assigned" and .enable_runner_id == 9) | .project_path' runner-changes.jsonl
```

## Shared options

| Flag | Effect |
| --- | --- |
| `--group` | Scan one group and its subgroups. |
| `--project` | Scan one project id or path. Repeat the flag for more than one project. Do not combine with `--group`. |
| `--membership` | Scan only projects the token's user belongs to. |
| `--include-archived` | Include archived projects. |
| `--verbose` | Print projects where the runner is not enabled. On `list_runner_projects.py` that is `--runner`; on `enable_runner.py` it is `--if-runner`. |

Both scripts exit with status 1 when a project could not be loaded or updated. A 403 while reading a project's runners is reported as inaccessible and does not fail the command. Ctrl+C prints `Interrupted` and the stats collected so far, then exits 130. Assigns that already succeeded stay applied; re-run with `--apply` to finish.
# gitlab-runner
