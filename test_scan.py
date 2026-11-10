#!/usr/bin/env python3
"""Tests for the project scan and project-runner guard."""

from __future__ import annotations

import unittest
import urllib.error
from io import BytesIO, StringIO
from unittest.mock import patch

import enable_runner
import list_runner_projects
from gitlab_api import (
    GitLabClient,
    GitLabError,
    INTERRUPT_EXIT,
    ProjectLoadError,
    RunnerScan,
    TOKEN_REJECTED,
    _next_link,
    _retry_delay,
    client_from_args,
    exit_gitlab_error,
    exit_interrupted,
    iter_projects_with_runner,
    raise_api_status,
    resolve_runner,
)


class FakeClient:
    def project_runner_ids(self, project_id: int) -> set[int]:
        return {6}


class MappingClient:
    def __init__(self, runners_by_project: dict[int, set[int]]) -> None:
        self.base_url = "https://gitlab.example.com"
        self.runners_by_project = runners_by_project

    def project_runner_ids(self, project_id: int) -> set[int]:
        return self.runners_by_project[project_id]

    def assign_runner(self, project_id: int, runner_id: int) -> None:
        self.runners_by_project.setdefault(project_id, set()).add(runner_id)


def _page_error():
    yield {"id": 1, "path_with_namespace": "a/one"}
    raise GitLabError(400, "bad page", "/projects")


def _named_projects():
    yield ProjectLoadError(GitLabError(404, "missing", "/projects/missing"))
    yield {"id": 1, "path_with_namespace": "a/one"}


class ScanTests(unittest.TestCase):
    def test_project_list_page_error_stops_the_scan(self) -> None:
        scan = RunnerScan()
        found = iter_projects_with_runner(FakeClient(), 6, _page_error(), False, scan)
        self.assertEqual(next(found)[0]["id"], 1)
        with self.assertRaises(GitLabError) as caught:
            next(found)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(scan.failed, 0)

    def test_bad_named_project_does_not_stop_the_scan(self) -> None:
        scan = RunnerScan()
        stderr = StringIO()
        with patch("sys.stderr", stderr):
            found = [
                project["path_with_namespace"]
                for project, _ids in iter_projects_with_runner(FakeClient(), 6, _named_projects(), False, scan)
            ]
        self.assertEqual(found, ["a/one"])
        self.assertIn("FAIL /projects/missing: missing", stderr.getvalue())
        self.assertEqual(scan.failed, 1)
        self.assertEqual(scan.enabled, 1)

    def test_missing_runner_type_is_rejected(self) -> None:
        runners = {
            "6": {"id": 6, "description": "old", "runner_type": None},
            "9": {"id": 9, "description": "new", "runner_type": "project_type"},
        }

        def resolve(_client, ident):
            return runners[ident]

        with patch("enable_runner.client_from_args", return_value=object()), patch(
            "enable_runner.resolve_runner", side_effect=resolve
        ):
            with self.assertRaises(SystemExit) as caught:
                enable_runner.main(["--gitlab-url", "https://gitlab.example.com", "--token", "t", "--if-runner", "6", "--enable-runner", "9"])
        self.assertIn("not a project runner", str(caught.exception))

    def test_verbose_lists_projects_without_the_runner(self) -> None:
        scan = RunnerScan()
        projects = [
            {"id": 1, "path_with_namespace": "a/one"},
            {"id": 2, "path_with_namespace": "a/two"},
        ]
        client = MappingClient({1: {6}, 2: {9}})
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            found = [
                project["path_with_namespace"]
                for project, _ids in iter_projects_with_runner(client, 6, projects, False, scan, True)
            ]
        self.assertEqual(found, ["a/one"])
        self.assertEqual(scan.skipped, 1)
        self.assertIn("SKIP a/two (id=2): runner #6 is not enabled", stdout.getvalue())


class ListRunnerProjectsTests(unittest.TestCase):
    def _run(self, argv: list[str], verbose: bool = False) -> tuple[int, str, str]:
        client = MappingClient({1: {6}, 2: {9}})
        runner = {"id": 6, "description": "old", "runner_type": "project_type"}
        projects = [
            {"id": 1, "path_with_namespace": "a/one"},
            {"id": 2, "path_with_namespace": "a/two"},
        ]
        stdout = StringIO()
        stderr = StringIO()
        extra = ["--verbose"] if verbose else []
        with patch("list_runner_projects.client_from_args", return_value=client), patch(
            "list_runner_projects.resolve_runner", return_value=runner
        ), patch("list_runner_projects.iter_projects", return_value=projects), patch(
            "sys.stdout", stdout
        ), patch("sys.stderr", stderr):
            status = list_runner_projects.main(argv + extra)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_default_prints_only_enabled_paths(self) -> None:
        status, stdout, stderr = self._run(
            ["--gitlab-url", "https://gitlab.example.com", "--token", "t", "--runner", "6"]
        )
        self.assertEqual(status, 0)
        self.assertEqual(stdout, "a/one\n")
        self.assertNotIn("SKIP", stdout)
        self.assertIn("skipped 1", stderr)

    def test_verbose_prints_enabled_and_skipped(self) -> None:
        status, stdout, stderr = self._run(
            ["--gitlab-url", "https://gitlab.example.com", "--token", "t", "--runner", "6"],
            verbose=True,
        )
        self.assertEqual(status, 0)
        self.assertIn("ENABLED a/one (id=1)", stdout)
        self.assertIn("SKIP a/two (id=2): runner #6 is not enabled", stdout)
        self.assertIn("skipped 1", stderr)

    def test_ctrl_c_prints_partial_summary(self) -> None:
        def projects():
            yield {"id": 1, "path_with_namespace": "a/one"}
            raise KeyboardInterrupt()

        client = MappingClient({1: {6}})
        runner = {"id": 6, "description": "old", "runner_type": "project_type"}
        stdout = StringIO()
        stderr = StringIO()
        with patch("list_runner_projects.client_from_args", return_value=client), patch(
            "list_runner_projects.resolve_runner", return_value=runner
        ), patch("list_runner_projects.iter_projects", return_value=projects()), patch(
            "sys.stdout", stdout
        ), patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as caught:
                list_runner_projects.main(
                    ["--gitlab-url", "https://gitlab.example.com", "--token", "t", "--runner", "6"]
                )
        self.assertEqual(caught.exception.code, INTERRUPT_EXIT)
        self.assertEqual(stdout.getvalue(), "a/one\n")
        self.assertIn("Interrupted.", stderr.getvalue())
        self.assertIn("Scanned 1 projects; runner enabled on 1", stderr.getvalue())


class Header(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class StubClient(GitLabClient):
    def __init__(self, responses):
        super().__init__("https://gitlab.example.com", "token")
        self.responses = list(responses)
        self.calls = []

    def _request(self, method, path, params=None, form=None, full_url=None):
        self.calls.append(full_url or dict(params or {}))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class PaginationTests(unittest.TestCase):
    def test_next_link_and_retry_cap(self) -> None:
        header = '<https://gitlab.example.com/api/v4/projects?id_after=1>; rel="next"'
        self.assertEqual(_next_link(header), "https://gitlab.example.com/api/v4/projects?id_after=1")
        self.assertEqual(_retry_delay("10000", 1), 120)

    def test_offset_pages_stop_when_the_next_page_does_not_advance(self) -> None:
        client = StubClient(
            [
                ([{"id": 1}], Header({"X-Next-Page": "1"})),
            ]
        )
        with self.assertRaises(GitLabError) as caught:
            list(client.paginate("/projects"))
        self.assertIn("did not advance", caught.exception.message)

    def test_keyset_follows_the_next_link(self) -> None:
        client = StubClient(
            [
                ([{"id": 1}], Header({"Link": '<https://gitlab.example.com/api/v4/projects?id_after=1>; rel="next"'})),
                ([{"id": 2}], Header({})),
            ]
        )
        ids = [item["id"] for item in client.paginate("/projects", {"pagination": "keyset"})]
        self.assertEqual(ids, [1, 2])
        self.assertEqual(client.calls[1], "https://gitlab.example.com/api/v4/projects?id_after=1")
        self.assertNotIn("page", client.calls[0])

    def test_keyset_rejects_a_next_link_on_another_host(self) -> None:
        client = StubClient(
            [
                ([{"id": 1}], Header({"Link": '<https://evil.example.net/api/v4/projects?id_after=1>; rel="next"'})),
            ]
        )
        with self.assertRaises(GitLabError) as caught:
            list(client.paginate("/projects", {"pagination": "keyset"}))
        self.assertIn("different host", caught.exception.message)
        self.assertEqual(len(client.calls), 1)

    def test_group_and_project_together_are_rejected(self) -> None:
        args = enable_runner.build_parser().parse_args(
            ["--gitlab-url", "https://gitlab.example.com", "--token", "t", "--group", "g", "--project", "g/p", "--if-runner", "1", "--enable-runner", "2"]
        )
        with self.assertRaises(SystemExit) as caught:
            client_from_args(args)
        self.assertIn("not both", str(caught.exception))


class ResolveVerboseTests(unittest.TestCase):
    def test_numeric_id_logs_the_lookup(self) -> None:
        client = StubClient([({"id": 6, "description": "old", "runner_type": "project_type"}, Header({}))])
        client.verbose = True
        stderr = StringIO()
        with patch("sys.stderr", stderr):
            runner = resolve_runner(client, "6")
        self.assertEqual(runner["id"], 6)
        self.assertIn("[verbose] resolving runner by id 6", stderr.getvalue())
        self.assertIn("[verbose] runner #6 found", stderr.getvalue())

    def test_description_logs_each_match_step(self) -> None:
        client = StubClient(
            [
                ([{"id": 6, "description": "old-runner", "name": "box"}], Header({})),
                ({"id": 6, "description": "old-runner", "runner_type": "project_type"}, Header({})),
            ]
        )
        client.verbose = True
        stderr = StringIO()
        with patch("sys.stderr", stderr):
            runner = resolve_runner(client, "old-runner")
        output = stderr.getvalue()
        self.assertEqual(runner["id"], 6)
        self.assertIn("[verbose] listing runners to match description 'old-runner'", output)
        self.assertIn("[verbose] 1 runners listed", output)
        self.assertIn("[verbose] 1 exact description match(es)", output)
        self.assertIn("[verbose] resolved 'old-runner' to runner #6", output)

    def test_missing_id_falls_back_to_description_search(self) -> None:
        client = StubClient([GitLabError(404, "Not Found", "/runners/6"), ([], Header({}))])
        client.verbose = True
        stderr = StringIO()
        with patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit):
                resolve_runner(client, "6")
        output = stderr.getvalue()
        self.assertIn("[verbose] no runner with id 6 (404), searching by description", output)
        self.assertIn("[verbose] 0 case-insensitive match(es)", output)


class UnauthorizedClient(GitLabClient):
    def __init__(self) -> None:
        super().__init__("https://gitlab.example.com", "bad-token")

    def _request(self, method, path, params=None, form=None, full_url=None):
        raise_api_status(401, "401 Unauthorized", full_url or path)


class NotFoundThenUnauthorized(GitLabClient):
    def __init__(self) -> None:
        super().__init__("https://gitlab.example.com", "bad-token")

    def _request(self, method, path, params=None, form=None, full_url=None):
        target = full_url or path
        if target.startswith("/runners/") and not target.startswith("/runners/all"):
            raise GitLabError(404, "Not Found", target)
        raise_api_status(401, "401 Unauthorized", target)


class AuthTests(unittest.TestCase):
    def test_unauthorized_status_exits_with_token_message(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            raise_api_status(401, "401 Unauthorized", "/runners/6")
        self.assertEqual(str(caught.exception), TOKEN_REJECTED)

    def test_other_statuses_stay_gitlab_errors(self) -> None:
        with self.assertRaises(GitLabError) as caught:
            raise_api_status(404, "Not Found", "/runners/6")
        self.assertEqual(caught.exception.status, 404)

    def test_exit_gitlab_error_maps_unauthorized(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            exit_gitlab_error(GitLabError(401, "Unauthorized", "/runners/6"))
        self.assertEqual(str(caught.exception), TOKEN_REJECTED)

    def test_invalid_token_is_not_reported_as_missing_runner(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            resolve_runner(UnauthorizedClient(), "6")
        self.assertEqual(str(caught.exception), TOKEN_REJECTED)

    def test_unauthorized_after_runner_404_is_still_a_token_error(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            resolve_runner(NotFoundThenUnauthorized(), "6")
        self.assertEqual(str(caught.exception), TOKEN_REJECTED)


class InterruptTests(unittest.TestCase):
    def test_exit_interrupted_prints_message_and_uses_status_130(self) -> None:
        stderr = StringIO()
        with patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as caught:
                exit_interrupted("Scanned 2 projects; runner enabled on 1")
        self.assertEqual(caught.exception.code, INTERRUPT_EXIT)
        self.assertEqual(
            stderr.getvalue(),
            "Interrupted.\nScanned 2 projects; runner enabled on 1\n",
        )

    def test_enable_runner_ctrl_c_keeps_partial_stats(self) -> None:
        def projects():
            yield {"id": 1, "path_with_namespace": "a/one"}
            raise KeyboardInterrupt()

        client = MappingClient({1: {6}})
        runners = {
            "6": {"id": 6, "description": "old", "runner_type": "project_type"},
            "9": {"id": 9, "description": "new", "runner_type": "project_type"},
        }
        stdout = StringIO()
        stderr = StringIO()
        with patch("enable_runner.client_from_args", return_value=client), patch(
            "enable_runner.resolve_runner", side_effect=lambda _client, ident: runners[ident]
        ), patch("enable_runner.iter_projects", return_value=projects()), patch(
            "sys.stdout", stdout
        ), patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as caught:
                enable_runner.main(
                    [
                        "--gitlab-url",
                        "https://gitlab.example.com",
                        "--token",
                        "t",
                        "--if-runner",
                        "6",
                        "--enable-runner",
                        "9",
                        "--apply",
                    ]
                )
        self.assertEqual(caught.exception.code, INTERRUPT_EXIT)
        self.assertIn("Interrupted.", stderr.getvalue())
        self.assertIn("Scanned 1 projects; runner enabled on 1", stderr.getvalue())
        self.assertIn("Assigns already completed stay applied. Re-run to finish.", stderr.getvalue())


class ScriptedClient:
    """Per-project scripted results for project scans and assignments."""

    def __init__(self, enabled=None, scan_errors=None, assign_errors=None) -> None:
        self.base_url = "https://gitlab.example.com"
        self.enabled = enabled or {}
        self.scan_errors = scan_errors or {}
        self.assign_errors = assign_errors or {}
        self.scanned_ids: list[int] = []
        self.assigned: list[tuple[int, int]] = []

    def project_runner_ids(self, project_id: int) -> set[int]:
        self.scanned_ids.append(project_id)
        if project_id in self.scan_errors:
            raise self.scan_errors[project_id]
        return set(self.enabled.get(project_id, set()))

    def assign_runner(self, project_id: int, runner_id: int) -> None:
        if project_id in self.assign_errors:
            raise self.assign_errors[project_id]
        self.assigned.append((project_id, runner_id))


class ScanAccessTests(unittest.TestCase):
    PROJECTS = [
        {"id": 1, "path_with_namespace": "a/one"},
        {"id": 2, "path_with_namespace": "a/two"},
    ]

    def test_unauthorized_during_scan_aborts_before_next_project(self) -> None:
        client = ScriptedClient(enabled={2: {6}}, scan_errors={1: SystemExit(TOKEN_REJECTED)})
        with self.assertRaises(SystemExit) as caught:
            list(iter_projects_with_runner(client, 6, self.PROJECTS, False, RunnerScan()))
        self.assertEqual(str(caught.exception), TOKEN_REJECTED)
        self.assertEqual(client.scanned_ids, [1])

    def test_forbidden_project_is_inaccessible_and_scan_continues(self) -> None:
        client = ScriptedClient(
            enabled={2: {6}},
            scan_errors={1: GitLabError(403, "Forbidden", "/projects/1/runners")},
        )
        scan = RunnerScan()
        stderr = StringIO()
        with patch("sys.stderr", stderr):
            found = [
                project["path_with_namespace"]
                for project, _ids in iter_projects_with_runner(client, 6, self.PROJECTS, False, scan)
            ]
        self.assertEqual(found, ["a/two"])
        self.assertEqual(scan.inaccessible, 1)
        self.assertEqual(scan.failed, 0)
        self.assertIn("INACCESSIBLE a/one (id=1): Forbidden", stderr.getvalue())


class EnableAssignTests(unittest.TestCase):
    PROJECT = [{"id": 1, "path_with_namespace": "a/one"}]

    def _run(self, client, projects, apply: bool = True):
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            stats = enable_runner.enable_on_projects(client, projects, 6, 9, apply, False, False)
        return stats, stdout.getvalue(), stderr.getvalue()

    def test_apply_assigns_the_runner(self) -> None:
        client = ScriptedClient(enabled={1: {6}})
        stats, stdout, _stderr = self._run(client, self.PROJECT)
        self.assertEqual(client.assigned, [(1, 9)])
        self.assertEqual(stats.assigned, 1)
        self.assertIn("ASSIGN a/one (id=1): enabled runner #9", stdout)

    def test_dry_run_does_not_assign(self) -> None:
        client = ScriptedClient(enabled={1: {6}})
        stats, stdout, _stderr = self._run(client, self.PROJECT, apply=False)
        self.assertEqual(client.assigned, [])
        self.assertEqual(stats.assigned, 0)
        self.assertEqual(stats.matched, 1)
        self.assertIn("MATCH a/one (id=1): runner #6 is enabled; would enable runner #9", stdout)

    def test_conflict_on_assign_counts_as_already_enabled(self) -> None:
        client = ScriptedClient(
            enabled={1: {6}},
            assign_errors={1: GitLabError(409, "Conflict", "/projects/1/runners")},
        )
        stats, stdout, _stderr = self._run(client, self.PROJECT)
        self.assertEqual(stats.already, 1)
        self.assertEqual(stats.assigned, 0)
        self.assertEqual(stats.failed, 0)
        self.assertIn("ALREADY a/one (id=1)", stdout)

    def test_other_assign_error_is_reported_as_failed(self) -> None:
        client = ScriptedClient(
            enabled={1: {6}},
            assign_errors={1: GitLabError(500, "boom", "/projects/1/runners")},
        )
        stats, _stdout, stderr = self._run(client, self.PROJECT)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.assigned, 0)
        self.assertIn("FAIL a/one (id=1): boom", stderr)

    def test_unauthorized_on_assign_stops_before_next_project(self) -> None:
        projects = [
            {"id": 1, "path_with_namespace": "a/one"},
            {"id": 2, "path_with_namespace": "a/two"},
        ]
        client = ScriptedClient(
            enabled={1: {6}, 2: {6}},
            assign_errors={1: SystemExit(TOKEN_REJECTED)},
        )
        with self.assertRaises(SystemExit) as caught:
            self._run(client, projects)
        self.assertEqual(str(caught.exception), TOKEN_REJECTED)
        self.assertEqual(client.scanned_ids, [1])
        self.assertEqual(client.assigned, [])


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200
        self.headers = Header({})

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class FakeOpener:
    """Returns or raises queued outcomes, one per request attempt."""

    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Header({"Retry-After": retry_after} if retry_after else {})
    return urllib.error.HTTPError(
        "https://gitlab.example.com/api/v4/runners/6", code, "error", headers, BytesIO(b'{"message": "busy"}')
    )


class RetryTests(unittest.TestCase):
    def _get(self, outcomes):
        opener = FakeOpener(outcomes)
        client = GitLabClient("https://gitlab.example.com", "t")
        with patch("urllib.request.build_opener", return_value=opener), patch("time.sleep") as sleep:
            payload = client.get_json("/runners/6")
        return payload, opener, sleep

    def test_rate_limit_waits_for_retry_after_then_succeeds(self) -> None:
        payload, opener, sleep = self._get([_http_error(429, "7"), FakeResponse(b'{"id": 6}')])
        self.assertEqual(payload, {"id": 6})
        self.assertEqual(len(opener.requests), 2)
        sleep.assert_called_once_with(7.0)

    def test_server_error_retries_with_default_backoff(self) -> None:
        payload, opener, sleep = self._get([_http_error(503), FakeResponse(b'{"id": 6}')])
        self.assertEqual(payload, {"id": 6})
        self.assertEqual(len(opener.requests), 2)
        sleep.assert_called_once_with(1.0)

    def test_network_error_is_retried(self) -> None:
        payload, opener, sleep = self._get(
            [urllib.error.URLError("connection reset"), FakeResponse(b'{"id": 6}')]
        )
        self.assertEqual(payload, {"id": 6})
        self.assertEqual(len(opener.requests), 2)
        sleep.assert_called_once_with(1.0)

    def test_body_read_timeout_is_retried(self) -> None:
        class TimedOutBody(FakeResponse):
            def read(self) -> bytes:
                raise TimeoutError("read timed out")

        payload, opener, sleep = self._get([TimedOutBody(b""), FakeResponse(b'{"id": 6}')])
        self.assertEqual(payload, {"id": 6})
        self.assertEqual(len(opener.requests), 2)
        sleep.assert_called_once_with(1.0)

    def test_unauthorized_is_not_retried(self) -> None:
        opener = FakeOpener([_http_error(401)])
        client = GitLabClient("https://gitlab.example.com", "bad")
        with patch("urllib.request.build_opener", return_value=opener), patch("time.sleep") as sleep:
            with self.assertRaises(SystemExit) as caught:
                client.get_json("/runners/6")
        self.assertEqual(str(caught.exception), TOKEN_REJECTED)
        self.assertEqual(len(opener.requests), 1)
        sleep.assert_not_called()

    def test_gives_up_after_six_attempts(self) -> None:
        opener = FakeOpener([_http_error(500) for _ in range(6)])
        client = GitLabClient("https://gitlab.example.com", "t")
        with patch("urllib.request.build_opener", return_value=opener), patch("time.sleep") as sleep:
            with self.assertRaises(SystemExit) as caught:
                client.get_json("/runners/6")
        self.assertIn("failed after retries", str(caught.exception))
        self.assertEqual(len(opener.requests), 6)
        self.assertEqual(sleep.call_count, 5)


if __name__ == "__main__":
    unittest.main()
