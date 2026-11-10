"""Shared GitLab API client and project scan for the runner scripts."""

from __future__ import annotations

import argparse
import email.utils
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, NoReturn, Optional


DEFAULT_PER_PAGE = 100
MAX_RETRY_DELAY = 120.0
USER_AGENT = "gitlab-runner-enable/1.0"
INTERRUPT_EXIT = 130
TOKEN_REJECTED = "GitLab rejected the token (401). Check GITLAB_TOKEN or --token."


class GitLabError(Exception):
    def __init__(self, status: int, message: str, path: str) -> None:
        super().__init__(f"GitLab API {status} {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


def raise_api_status(status: int, message: str, path: str) -> NoReturn:
    if status == 401:
        raise SystemExit(TOKEN_REJECTED)
    raise GitLabError(status, message, path)


def exit_gitlab_error(error: GitLabError) -> NoReturn:
    if error.status == 401:
        raise SystemExit(TOKEN_REJECTED) from error
    print(str(error), file=sys.stderr)
    raise SystemExit(1) from error


def exit_interrupted(*lines: str) -> NoReturn:
    print("Interrupted.", file=sys.stderr)
    for line in lines:
        if line:
            print(line, file=sys.stderr)
    raise SystemExit(INTERRUPT_EXIT)


class GitLabClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0, verbose: bool = False) -> None:
        base = base_url.strip().rstrip("/")
        if base.endswith("/api/v4"):
            base = base[: -len("/api/v4")]
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SystemExit(f"Invalid GitLab URL: {base_url}")
        self.base_url = base
        self.token = token
        self.timeout = timeout
        self.verbose = verbose

    def get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        payload, _headers = self._request("GET", path, params=params)
        return payload

    def paginate(self, path: str, params: Optional[dict[str, Any]] = None) -> Iterator[dict[str, Any]]:
        query = dict(params or {})
        query.setdefault("per_page", DEFAULT_PER_PAGE)
        keyset = str(query.get("pagination", "")) == "keyset"
        page = 1
        next_url: Optional[str] = None
        seen_urls: set[str] = set()
        while True:
            if next_url:
                payload, headers = self._request("GET", path, full_url=next_url)
            else:
                if not keyset:
                    query["page"] = page
                payload, headers = self._request("GET", path, params=query)
            if not isinstance(payload, list):
                raise GitLabError(200, "expected a JSON list", path)
            yield from payload
            link = _next_link(headers.get("Link"))
            if link:
                if not _same_origin(link, self.base_url):
                    raise GitLabError(200, "pagination link points to a different host", path)
                if link in seen_urls:
                    raise GitLabError(200, "pagination next link repeated", path)
                seen_urls.add(link)
                next_url = link
                continue
            raw_next = headers.get("X-Next-Page")
            if not raw_next:
                return
            try:
                nxt = int(raw_next)
            except ValueError as exc:
                raise GitLabError(200, f"invalid X-Next-Page header {raw_next!r}", path) from exc
            if nxt <= page:
                raise GitLabError(200, f"X-Next-Page {nxt} did not advance past page {page}", path)
            page = nxt
            keyset = False
            next_url = None

    def get_runner(self, runner_id: int) -> dict[str, Any]:
        payload = self.get_json(f"/runners/{runner_id}")
        if not isinstance(payload, dict) or "id" not in payload:
            raise GitLabError(200, "runner response was missing an id", f"/runners/{runner_id}")
        return payload

    def list_runners(self) -> list[dict[str, Any]]:
        try:
            return list(self.paginate("/runners/all"))
        except GitLabError as exc:
            if exc.status != 403:
                raise
            vlog(self.verbose, "GET /runners/all -> 403, falling back to GET /runners")
            return list(self.paginate("/runners"))

    def list_projects(self, group: Optional[str], include_archived: bool, membership: bool) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "simple": "true",
            "pagination": "keyset",
            "order_by": "id",
            "sort": "asc",
            "per_page": DEFAULT_PER_PAGE,
        }
        if not include_archived:
            params["archived"] = "false"
        if membership:
            params["membership"] = "true"
        if group:
            encoded = urllib.parse.quote(group, safe="")
            params["include_subgroups"] = "true"
            yield from self.paginate(f"/groups/{encoded}/projects", params)
            return
        yield from self.paginate("/projects", params)

    def get_project(self, ident: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(str(ident), safe="")
        payload = self.get_json(f"/projects/{encoded}")
        if not isinstance(payload, dict) or "id" not in payload:
            raise GitLabError(200, "project response was missing an id", f"/projects/{encoded}")
        return payload

    def project_runner_ids(self, project_id: int) -> set[int]:
        ids: set[int] = set()
        runner_path = f"/projects/{project_id}/runners"
        for runner in self.paginate(runner_path):
            runner_id = runner.get("id")
            if runner_id is None:
                raise GitLabError(200, "runner response was missing an id", runner_path)
            ids.add(int(runner_id))
        return ids

    def assign_runner(self, project_id: int, runner_id: int) -> None:
        self._request("POST", f"/projects/{project_id}/runners", form={"runner_id": runner_id})

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        form: Optional[dict[str, Any]] = None,
        full_url: Optional[str] = None,
    ) -> tuple[Any, Mapping[str, str]]:
        url = full_url or (self.base_url + "/api/v4" + path)
        error_path = full_url or path
        if params and not full_url:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = None
        headers = {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        delay = 1.0
        last_error: Optional[BaseException] = None
        opener = urllib.request.build_opener(_RejectRedirect)
        for _attempt in range(6):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    try:
                        payload = json.loads(raw.decode() or "null")
                    except json.JSONDecodeError as exc:
                        raise GitLabError(response.status, "response was not JSON", error_path) from exc
                    return payload, response.headers
            except GitLabError:
                raise
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")
                exc.close()
                message = _error_message(body) or exc.reason
                if exc.code == 429 or exc.code >= 500:
                    last_error = GitLabError(exc.code, str(message), error_path)
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if _attempt < 5:
                        time.sleep(_retry_delay(retry_after, delay))
                        delay = min(delay * 2, 30)
                    continue
                raise_api_status(exc.code, str(message), error_path)
            except OSError as exc:  # URLError is an OSError; this also covers body-read errors
                last_error = exc
                if _attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                continue
        raise SystemExit(f"GitLab API request failed after retries: {method} {error_path}: {last_error}")


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise GitLabError(code, f"unexpected redirect to {newurl}", req.full_url)


def _next_link(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    for part in header.split(","):
        pieces = [piece.strip() for piece in part.split(";")]
        if not pieces or not pieces[0].startswith("<") or not pieces[0].endswith(">"):
            continue
        rels = {piece for piece in pieces[1:]}
        if 'rel="next"' in rels or "rel='next'" in rels or "rel=next" in rels:
            return pieces[0][1:-1]
    return None


def _same_origin(url: str, base_url: str) -> bool:
    target = urllib.parse.urlparse(url)
    origin = urllib.parse.urlparse(base_url)
    return (target.scheme.lower(), target.netloc.lower()) == (origin.scheme.lower(), origin.netloc.lower())


def _retry_delay(retry_after: Optional[str], fallback: float) -> float:
    if not retry_after:
        return fallback
    try:
        return min(MAX_RETRY_DELAY, max(0.0, float(retry_after)))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(retry_after)
    except (TypeError, ValueError):
        return fallback
    if parsed is None:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = (parsed - datetime.now(timezone.utc)).total_seconds()
    return min(MAX_RETRY_DELAY, max(0.0, seconds))


def _error_message(body: str) -> str:
    body = body.strip()
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:500]
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if isinstance(message, (dict, list)):
            return json.dumps(message)[:500]
        if message:
            return str(message)[:500]
    return body[:500]


def runner_label(runner: Mapping[str, Any]) -> str:
    description = runner.get("description") or runner.get("name") or "(no description)"
    runner_type = runner.get("runner_type") or "unknown"
    paused = runner.get("paused")
    return f"#{runner.get('id')} {description!r} ({runner_type}, paused={paused})"


def project_label(project: Mapping[str, Any]) -> str:
    path = project.get("path_with_namespace") or project.get("path") or "?"
    return f"{path} (id={project.get('id')})"


def resolve_runner(client: GitLabClient, ident: str) -> dict[str, Any]:
    ident = ident.strip()
    if not ident:
        raise SystemExit("Runner id or description cannot be empty.")
    if ident.isdigit():
        vlog(client.verbose, f"resolving runner by id {ident}")
        try:
            runner = client.get_runner(int(ident))
            vlog(client.verbose, f"runner #{ident} found")
            return runner
        except GitLabError as exc:
            if exc.status != 404:
                raise
            vlog(client.verbose, f"no runner with id {ident} (404), searching by description")

    vlog(client.verbose, f"listing runners to match description {ident!r}")
    runners = client.list_runners()
    vlog(client.verbose, f"{len(runners)} runners listed")

    def names(runner: Mapping[str, Any]) -> set[str]:
        return {str(runner.get("description") or ""), str(runner.get("name") or "")}

    matches = [runner for runner in runners if ident in names(runner)]
    vlog(client.verbose, f"{len(matches)} exact description match(es)")
    if not matches:
        lowered = ident.casefold()
        matches = [runner for runner in runners if lowered in {name.casefold() for name in names(runner)}]
        vlog(client.verbose, f"{len(matches)} case-insensitive match(es)")
    if not matches:
        raise SystemExit(f"No runner found with id or description {ident!r}.")
    if len(matches) > 1:
        found = ", ".join(runner_label(runner) for runner in matches)
        raise SystemExit(f"Runner description {ident!r} matches more than one runner: {found}")
    runner_id = int(matches[0]["id"])
    vlog(client.verbose, f"resolved {ident!r} to runner #{runner_id}")
    return client.get_runner(runner_id)


@dataclass(frozen=True)
class ProjectLoadError:
    error: GitLabError


def iter_projects(client: GitLabClient, args: argparse.Namespace) -> Iterable[Any]:
    if args.project:
        for ident in args.project:
            try:
                yield client.get_project(ident)
            except GitLabError as exc:
                if exc.status == 401:
                    raise
                yield ProjectLoadError(exc)
        return
    yield from client.list_projects(args.group, args.include_archived, args.membership)


@dataclass
class RunnerScan:
    scanned: int = 0
    enabled: int = 0
    skipped: int = 0
    failed: int = 0
    inaccessible: int = 0


def iter_projects_with_runner(
    client: GitLabClient,
    runner_id: int,
    projects: Iterable[Any],
    include_archived: bool,
    stats: RunnerScan,
    verbose: bool = False,
) -> Iterator[tuple[Mapping[str, Any], set[int]]]:
    """Yield each project where runner_id is enabled, plus that project's runner ids."""
    pending = iter(projects)
    while True:
        try:
            project = next(pending)
        except StopIteration:
            return
        if isinstance(project, ProjectLoadError):
            stats.failed += 1
            print(f"FAIL {project.error.path}: {project.error.message}", file=sys.stderr)
            continue
        if not include_archived and project.get("archived") is True:
            print(f"SKIP {project_label(project)}: archived", file=sys.stderr)
            continue
        project_id = project.get("id")
        if project_id is None:
            stats.failed += 1
            print(f"FAIL {project_label(project)}: project response was missing an id", file=sys.stderr)
            continue
        stats.scanned += 1
        label = project_label(project)
        try:
            enabled_ids = client.project_runner_ids(int(project_id))
        except GitLabError as exc:
            if exc.status == 401:
                raise
            if exc.status == 403:
                stats.inaccessible += 1
                print(f"INACCESSIBLE {label}: {exc.message}", file=sys.stderr)
            else:
                stats.failed += 1
                print(f"FAIL {label}: {exc.message}", file=sys.stderr)
            _progress(stats.scanned, stats.enabled)
            continue
        if runner_id in enabled_ids:
            stats.enabled += 1
            yield project, enabled_ids
        else:
            stats.skipped += 1
            if verbose:
                print(f"SKIP {label}: runner #{runner_id} is not enabled")
        _progress(stats.scanned, stats.enabled)


def add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gitlab-url",
        default=os.environ.get("GITLAB_URL", ""),
        help="GitLab instance URL. Defaults to GITLAB_URL.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITLAB_TOKEN", ""),
        help="GitLab token with the api scope. Defaults to GITLAB_TOKEN.",
    )
    parser.add_argument(
        "--group",
        help="Only scan this group id or path, including subgroups.",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Only scan this project id or path. Can be repeated.",
    )
    parser.add_argument(
        "--membership",
        action="store_true",
        help="Only scan projects the token's user is a member of.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived projects.",
    )


def client_from_args(args: argparse.Namespace) -> GitLabClient:
    if getattr(args, "project", None) and args.group:
        raise SystemExit("Pass either --group or --project, not both.")
    if not args.gitlab_url:
        raise SystemExit("Set GITLAB_URL or pass --gitlab-url.")
    if not args.token:
        raise SystemExit("Set GITLAB_TOKEN or pass --token.")
    return GitLabClient(args.gitlab_url, args.token, verbose=getattr(args, "verbose", False))


def vlog(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[verbose] {message}", file=sys.stderr, flush=True)


def _progress(scanned: int, enabled: int) -> None:
    if scanned % 25 != 0:
        return
    print(
        f"checked {scanned} projects, {enabled} with runner enabled",
        file=sys.stderr,
    )
