# Shared steps (gitlab_api.py)

Used by both `enable_runner.py` and `list_runner_projects.py`.

```text
client_from_args(args):
    require --gitlab-url / GITLAB_URL and --token / GITLAB_TOKEN
    reject --group together with --project
    return GitLabClient(url, token, verbose=args.verbose)

resolve_runner(client, ident):
    if ident is numeric:
        GET /runners/:id; return if found
        on 404, fall through to description search
    list all runners (GET /runners/all, falls back to /runners on 403)
    match by exact description or name; then case-insensitive
    error if no match or more than one match
    return GET /runners/:id
    verbose: log each step and match counts to stderr

iter_projects(client, args):
    --project given: GET each project by id or path
        on failure, yield a load error (401 aborts)
    otherwise: list projects (keyset pagination, ordered by id)
        --group given: GET /groups/:group/projects with include_subgroups=true
        no --group: GET /projects
        archived=false unless --include-archived
        membership=true with --membership

project_runner_ids(client, project_id):
    GET /projects/:id/runners, following next links until no pages remain

request retry:
    up to 6 attempts
    retry on 429 and 5xx using Retry-After or exponential backoff
    retry on network errors
    401 aborts the run; 3xx redirects are rejected
    next-page links must point to the --gitlab-url host, otherwise abort
```
