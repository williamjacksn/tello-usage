"""Synthetic account pages: no credentials or HAR data are stored in tests."""

import unittest
from collections.abc import Iterable
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx2

from tello_usage import Balance, TelloClient, TelloError


def fields(token: str) -> str:
    return (
        f'<input type="hidden" name="_token" value="{token}">'
        f'<input type="text" name="_nothing_{token}" value="">'
        '<input type="hidden" name="_snowman" value="&#9731;">'
    )


LOGIN = (
    '<form id="account_login_form" method="post" action="/account/process_login">'
    + fields("login")
    + '<input name="username"><input type="password" name="password"></form>'
)


def account(selected: str, remaining: str = "5.46 GB") -> str:
    renewal_day = {"a": 22, "b": 24, "c": 25, "d": 23}[selected]
    options = "".join(
        f'<option value="{sid}" {"selected" if sid == selected else ""}>'
        f"Line {sid}</option>"
        for sid in ("a", "b", "c", "d")
    )
    return (
        '<form method="post" action="/account/process_home">'
        + fields(selected)
        + '<input name="action" value="change_selected_subscription" type="hidden">'
        '<input name="redirect_to" value="account/home" type="hidden">'
        f'<select name="selected_subscription_id">{options}</select></form>'
        '<div class="balance-details"><div class="balance-from">'
        "of 300 minutes</div></div>"
        '<div class="progress_holder"><div class="balance-details">'
        '<div class="balance-from">'
        f"{remaining} remaining / 10 GB</div></div>"
        '<div class="more-details"><div class="valid-date">Renewal date: '
        f"<span>10/{renewal_day}/2026</span></div></div></div>"
        # An unrelated purchase form must never be submitted.
        '<form method="post" action="/process_buy">'
        '<input name="action" value="buy"></form>'
    )


class ClientTests(unittest.TestCase):
    def test_billing_cycle_days(self) -> None:
        with patch("tello_usage.client.date", wraps=date) as clock:
            clock.today.return_value = date(2026, 10, 19)
            for renewal, expected in [
                ("2026-10-23", 4),
                ("2026-10-20", 1),
                ("2026-10-19", 0),
                ("2026-10-18", 0),
                (None, None),
            ]:
                with self.subTest(renewal=renewal):
                    self.assertEqual(
                        Balance("a", "Line a", "1 GB", "10 GB", renewal).days_left,
                        expected,
                    )
            clock.today.return_value = date(2026, 12, 31)
            self.assertEqual(
                Balance("a", "Line a", "1 GB", "10 GB", "2027-01-02").days_left, 2
            )
            clock.today.return_value = date(2028, 2, 28)
            self.assertEqual(
                Balance("a", "Line a", "1 GB", "10 GB", "2028-03-01").days_left, 2
            )

    def test_data_renewal_date_is_scoped_to_data_balance(self) -> None:
        from tello_usage.client import _account_page, _balance

        html = '<div class="valid-date">Renewal date: 01/01/2099</div>' + account("a")
        balance = _balance(_account_page(html), "a", "Line a")
        self.assertEqual(balance.renewal_date, "2026-10-22")

    def test_unavailable_billing_date(self) -> None:
        from tello_usage.client import _account_page, _balance

        for text in ["", "02/30/2026", "unknown"]:
            with self.subTest(text=text):
                balance = _balance(
                    _account_page(account("a").replace("10/22/2026", text)),
                    "a",
                    "Line a",
                )
                self.assertIsNone(balance.renewal_date)
                self.assertIsNone(balance.days_left)

    def test_percentage_available(self) -> None:
        cases = [
            ("5.46 GB", "10 GB", 54.6),
            ("457.59 MB", "10 GB", 4.58),
            ("373.35 MB", "2 GB", 18.67),
            ("0 MB", "10 GB", 0.0),
            ("10 GB", "10 GB", 100.0),
            ("12 GB", "10 GB", 100.0),
            ("1,000 MB", "2 GB", 50.0),
            ("500 kb", "1 MB", 50.0),
            ("500 GB", "1 TB", 50.0),
            ("0 MB", "0 GB", None),
            ("1 GB", "Unlimited", None),
        ]
        for remaining, allowance, expected in cases:
            with self.subTest(remaining=remaining, allowance=allowance):
                self.assertEqual(
                    Balance("a", "Line a", remaining, allowance).percentage_available,
                    expected,
                )

    def test_login_cookies_rotating_tokens_and_all_four_lines(self) -> None:
        current = "d"
        switches = []
        pending = LOGIN

        def handle(request: httpx2.Request) -> httpx2.Response:
            nonlocal current, pending
            path = request.url.path
            if request.method == "GET" and path == "/account/login":
                return httpx2.Response(
                    200, text=LOGIN, headers={"set-cookie": "session=fake; Path=/"}
                )
            self.assertIn("session=fake", request.headers["cookie"])
            if request.method == "GET" and path == "/account/home":
                return httpx2.Response(200, text=pending)
            self.assertEqual(request.method, "POST")
            data = parse_qs(request.content.decode(), keep_blank_values=True)
            self.assertEqual(data["_snowman"], ["\u2603"])
            self.assertEqual(request.headers["origin"], "https://tello.com")
            if path == "/account/process_login":
                self.assertEqual(data["_token"], ["login"])
                self.assertEqual(data["username"], ["test@example.invalid"])
                self.assertEqual(data["password"], ["fake password"])
                self.assertEqual(data["_nothing_login"], [""])
            else:
                self.assertEqual(path, "/account/process_home")
                self.assertEqual(data["_token"], [current])
                self.assertEqual(data[f"_nothing_{current}"], [""])
                self.assertEqual(data["action"], ["change_selected_subscription"])
                self.assertNotIn("password", data)
                current = data["selected_subscription_id"][0]
                switches.append(current)
            pending = account(current, "457.59 MB" if current == "b" else "5.46 GB")
            return httpx2.Response(302, headers={"location": "/account/home"})

        with TelloClient(transport=httpx2.MockTransport(handle)) as client:
            balances = client.balances("test@example.invalid", "fake password")
        self.assertEqual([b.subscription_id for b in balances], ["a", "b", "c", "d"])
        self.assertEqual(switches, ["a", "b", "c"])
        self.assertEqual(balances[1].remaining, "457.59 MB")
        self.assertEqual(balances[1].allowance, "10 GB")
        self.assertEqual(
            [b.renewal_date for b in balances],
            [
                "2026-10-22",
                "2026-10-24",
                "2026-10-25",
                "2026-10-23",
            ],
        )

    def run_pages(self, pages: Iterable[str]) -> list[Balance]:
        pages = iter(pages)
        transport = httpx2.MockTransport(
            lambda request: httpx2.Response(200, text=next(pages))
        )
        with TelloClient(transport=transport) as client:
            return client.balances("test@example.invalid", "fake")

    def test_rejected_login(self) -> None:
        with self.assertRaisesRegex(TelloError, "login failed"):
            self.run_pages([LOGIN, LOGIN])

    def test_switch_failure_does_not_mislabel_balance(self) -> None:
        with self.assertRaisesRegex(TelloError, "did not switch"):
            self.run_pages([LOGIN, account("d"), account("d")])

    def test_session_expiration_during_switch(self) -> None:
        with self.assertRaisesRegex(TelloError, "session expired"):
            self.run_pages([LOGIN, account("d"), LOGIN])

    def test_missing_data_is_not_reported_as_zero(self) -> None:
        with self.assertRaisesRegex(TelloError, "Could not find"):
            self.run_pages(
                [LOGIN, account("d").replace("5.46 GB remaining / 10 GB", "")]
            )

    def test_missing_csrf_token(self) -> None:
        with self.assertRaisesRegex(TelloError, "CSRF"):
            self.run_pages([LOGIN.replace('name="_token"', 'name="absent"')])

    def test_external_form_is_rejected(self) -> None:
        with self.assertRaisesRegex(TelloError, "form action"):
            self.run_pages(
                [
                    LOGIN.replace(
                        "/account/process_login", "https://example.invalid/login"
                    )
                ]
            )

    def test_external_redirect_is_rejected_before_request(self) -> None:
        calls = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            calls.append(request.url.host)
            return httpx2.Response(
                307, headers={"location": "https://example.invalid/login"}
            )

        with (
            TelloClient(transport=httpx2.MockTransport(handle)) as client,
            self.assertRaisesRegex(TelloError, "redirected outside"),
        ):
            client.balances("test@example.invalid", "fake")
        self.assertEqual(calls, ["tello.com"])

    def test_zero_balance(self) -> None:
        from tello_usage.client import _account_page, _balance

        balance = _balance(_account_page(account("a", "0 MB")), "a", "Line a")
        self.assertEqual(balance.remaining, "0 MB")


if __name__ == "__main__":
    unittest.main()
