"""Saving and reusing login sessions against a simulated Tello Mobile website."""

import contextlib
import functools
import io
import json
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx2
from test_client import LOGIN, account

from tello_usage import TelloClient, TelloError, TelloLoginRequired
from tello_usage.cli import main

USERNAME = "test@example.invalid"
PASSWORD = "fake password"  # noqa: S105


class FakeTello:
    """Issues a browser-session cookie on login and honors it until expired."""

    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self.current = "d"
        self.issued = 0

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def _session_cookie(self) -> dict[str, str]:
        self.issued += 1
        return {
            "set-cookie": f"laravel_session=s{self.issued}; Path=/; Secure; HttpOnly"
        }

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        cookie = SimpleCookie(request.headers.get("cookie", ""))
        session = cookie["laravel_session"].value if "laravel_session" in cookie else ""
        logged_in = session in self.sessions
        if method == "GET" and path == "/account/login":
            return httpx2.Response(200, text=LOGIN, headers=self._session_cookie())
        if method == "POST" and path == "/account/process_login":
            data = parse_qs(request.content.decode())
            if data.get("username") != [USERNAME] or data.get("password") != [PASSWORD]:
                return httpx2.Response(200, text=LOGIN)
            headers = self._session_cookie() | {"location": "/account/home"}
            self.sessions.add(f"s{self.issued}")
            return httpx2.Response(302, headers=headers)
        if not logged_in:
            return httpx2.Response(302, headers={"location": "/account/login"})
        if method == "GET" and path == "/account/home":
            return httpx2.Response(200, text=account(self.current))
        if method == "POST" and path == "/account/process_home":
            data = parse_qs(request.content.decode())
            self.current = data["selected_subscription_id"][0]
            return httpx2.Response(302, headers={"location": "/account/home"})
        return httpx2.Response(404)

    def logins(self) -> int:
        return self.requests.count(("POST", "/account/process_login"))


class CookieFileTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "cookies.json"
        self.server = FakeTello()

    def client(self) -> TelloClient:
        return TelloClient(transport=self.server.transport(), cookie_file=self.path)

    def log_in_and_save(self) -> None:
        with self.client() as client:
            client.balances(USERNAME, PASSWORD)

    def test_saved_session_is_reused_without_credentials(self) -> None:
        self.log_in_and_save()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["username"], USERNAME)
        self.assertEqual([c["name"] for c in saved["cookies"]], ["laravel_session"])
        self.assertTrue(saved["cookies"][0]["http_only"])
        self.assertNotIn(PASSWORD, self.path.read_text(encoding="utf-8"))
        self.server.requests.clear()

        with self.client() as client:
            balances = client.balances()
        self.assertEqual([b.subscription_id for b in balances], ["a", "b", "c", "d"])
        self.assertEqual(self.server.logins(), 0)
        self.assertNotIn(("GET", "/account/login"), self.server.requests)

    def test_saved_session_is_reused_for_same_user_with_credentials(self) -> None:
        self.log_in_and_save()
        with self.client() as client:
            client.balances(USERNAME.upper(), PASSWORD)
        self.assertEqual(self.server.logins(), 1)

    def test_expired_session_requires_credentials_then_logs_in(self) -> None:
        self.log_in_and_save()
        old = self.path.read_text(encoding="utf-8")
        self.server.expire_sessions()
        with self.client() as client:
            with self.assertRaises(TelloLoginRequired):
                client.balances()
            self.server.requests.clear()
            balances = client.balances(USERNAME, PASSWORD)
        self.assertEqual(len(balances), 4)
        # The expired session is not checked again before logging in.
        self.assertEqual(self.server.requests[0], ("GET", "/account/login"))
        self.assertNotEqual(self.path.read_text(encoding="utf-8"), old)
        with self.client() as client:
            client.balances()
        self.assertEqual(self.server.logins(), 1)

    def test_session_for_another_user_is_not_reused(self) -> None:
        self.log_in_and_save()
        self.server.requests.clear()
        with self.client() as client, self.assertRaisesRegex(TelloError, "failed"):
            client.balances("other@example.invalid", "other password")
        self.assertEqual(self.server.requests[0], ("GET", "/account/login"))

    def test_failed_run_keeps_existing_file(self) -> None:
        self.log_in_and_save()
        old = self.path.read_text(encoding="utf-8")
        self.server.expire_sessions()
        with self.client() as client, self.assertRaisesRegex(TelloError, "failed"):
            client.balances(USERNAME, "wrong password")
        self.assertEqual(self.path.read_text(encoding="utf-8"), old)

    def test_invalid_files_are_ignored(self) -> None:
        self.path.parent.mkdir(parents=True)
        cookie = {
            "name": "laravel_session",
            "value": "s1",
            "domain": "tello.com",
            "domain_specified": False,
            "domain_initial_dot": False,
            "path": "/",
            "path_specified": True,
            "secure": True,
            "expires": None,
            "discard": True,
            "http_only": True,
        }
        for content in [
            "not json",
            "[]",
            json.dumps({"version": 2, "username": USERNAME, "cookies": [cookie]}),
            json.dumps({"version": 1, "username": "", "cookies": [cookie]}),
            json.dumps({"version": 1, "username": USERNAME, "cookies": [{}]}),
            json.dumps(
                {
                    "version": 1,
                    "username": USERNAME,
                    "cookies": [cookie | {"domain": "example.invalid"}],
                }
            ),
            json.dumps(
                {
                    "version": 1,
                    "username": USERNAME,
                    "cookies": [cookie | {"expires": 1}],
                }
            ),
        ]:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.client() as client:
                    self.assertFalse(client.load_cookies(self.path))
                    with self.assertRaises(TelloLoginRequired):
                        client.balances()
        self.assertEqual(self.server.requests, [])

    def test_missing_file_starts_fresh_session(self) -> None:
        with TelloClient(transport=self.server.transport()) as client:
            self.assertFalse(client.load_cookies(self.path))
            with self.assertRaises(TelloLoginRequired):
                client.save_cookies(self.path)
        self.assertFalse(self.path.exists())

    def test_save_failure_warns_and_returns_balances(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()  # A directory cannot be replaced by the cookie file.
        with self.client() as client, self.assertWarnsRegex(RuntimeWarning, "save"):
            balances = client.balances(USERNAME, PASSWORD)
        self.assertEqual(len(balances), 4)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])


class CookieCommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "cookies.json"
        self.server = FakeTello()

    def run_cli(self, *args: str, env: dict[str, str] | None = None) -> tuple[str, str]:
        client = functools.partial(TelloClient, transport=self.server.transport())
        with (
            patch("sys.argv", ["tello-usage", *args]),
            patch.dict("os.environ", env or {}, clear=True),
            patch("sys.stdin.isatty", return_value=False),
            patch("tello_usage.cli.TelloClient", client),
            patch("tello_usage.cli.getpass", side_effect=AssertionError("prompted")),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            main()
        return output.getvalue(), error.getvalue()

    def credentials(self) -> dict[str, str]:
        return {"TELLO_USERNAME": USERNAME, "TELLO_PASSWORD": PASSWORD}

    def test_reuses_saved_session_without_credentials(self) -> None:
        self.run_cli("--cookie-file", str(self.path), env=self.credentials())
        self.assertTrue(self.path.exists())
        output, _ = self.run_cli("--json", "--cookie-file", str(self.path))
        self.assertEqual(len(json.loads(output)), 4)
        self.assertEqual(self.server.logins(), 1)

    def test_environment_cookie_file(self) -> None:
        env = self.credentials() | {"TELLO_COOKIE_FILE": str(self.path)}
        self.run_cli(env=env)
        self.run_cli(env={"TELLO_COOKIE_FILE": str(self.path)})
        self.assertEqual(self.server.logins(), 1)

    def test_expired_session_falls_back_to_credentials(self) -> None:
        self.run_cli("--cookie-file", str(self.path), env=self.credentials())
        self.server.expire_sessions()
        self.run_cli("--cookie-file", str(self.path), env=self.credentials())
        self.assertEqual(self.server.logins(), 2)
        self.run_cli("--cookie-file", str(self.path))
        self.assertEqual(self.server.logins(), 2)

    def test_expired_session_without_credentials_reports_error(self) -> None:
        self.run_cli("--cookie-file", str(self.path), env=self.credentials())
        self.server.expire_sessions()
        with self.assertRaises(SystemExit) as raised:
            self.run_cli("--cookie-file", str(self.path))
        self.assertEqual(raised.exception.code, 2)

    def test_unwritable_cookie_file_warns(self) -> None:
        self.path.mkdir()
        output, error = self.run_cli(
            "--cookie-file", str(self.path), env=self.credentials()
        )
        self.assertIn("remaining", output)
        self.assertIn("Warning: Could not save cookies", error)


if __name__ == "__main__":
    unittest.main()
