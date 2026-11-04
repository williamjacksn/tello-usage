import argparse
import json
import os
import sys
from dataclasses import asdict
from getpass import getpass

import httpx2

from .client import Balance, TelloClient, TelloError, TelloLoginRequired


def _credentials(
    parser: argparse.ArgumentParser, username: str | None
) -> tuple[str, str]:
    password = os.environ.get("TELLO_PASSWORD")
    if not username:
        if not sys.stdin.isatty():
            parser.error(
                "set TELLO_USERNAME or pass --username when running without a terminal"
            )
        username = input("Tello Mobile email: ").strip()
    if not password:
        if not sys.stdin.isatty():
            parser.error("set TELLO_PASSWORD when running without a terminal")
        password = getpass("Tello Mobile password: ")
    if not username or not password:
        parser.error("username and password must not be empty")
    return username, password


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Get remaining data balances for all Tello Mobile lines."
    )
    parser.add_argument(
        "--username",
        help="Tello Mobile email address (defaults to TELLO_USERNAME or a prompt)",
    )
    parser.add_argument(
        "--cookie-file",
        metavar="PATH",
        help="save the login session to this file and reuse it on later runs "
        "(defaults to TELLO_COOKIE_FILE; not saved if unset)",
    )
    parser.add_argument("--json", action="store_true", help="Print balances as JSON")
    args = parser.parse_args()
    username = args.username or os.environ.get("TELLO_USERNAME") or None
    cookie_file = args.cookie_file or os.environ.get("TELLO_COOKIE_FILE") or None
    try:
        with TelloClient() as client:
            balances: list[Balance] | None = None
            if cookie_file and client.load_cookies(cookie_file):
                try:
                    balances = client.balances(username)
                except TelloLoginRequired:
                    pass
            if balances is None:
                balances = client.balances(*_credentials(parser, username))
            if cookie_file:
                try:
                    client.save_cookies(cookie_file)
                except OSError as error:
                    print(
                        f"Warning: Could not save cookies to {cookie_file}: {error}",
                        file=sys.stderr,
                    )
    except (EOFError, KeyboardInterrupt):
        parser.exit(130, "\nCanceled.\n")
    except TelloError as error:
        parser.exit(1, f"Error: {error}\n")
    except httpx2.HTTPStatusError as error:
        parser.exit(
            1, f"Error: Tello Mobile returned HTTP {error.response.status_code}.\n"
        )
    except httpx2.RequestError:
        parser.exit(
            1,
            "Error: Could not connect to Tello Mobile. "
            "Check your network connection and try again.\n",
        )
    if args.json:
        print(
            json.dumps(
                [
                    asdict(balance)
                    | {
                        "percentage_available": balance.percentage_available,
                        "days_left": balance.days_left,
                    }
                    for balance in balances
                ],
                indent=2,
            )
        )
    else:
        for balance in balances:
            available = balance.percentage_available
            percentage = (
                f"{available:.2f}% available"
                if available is not None
                else "percentage unavailable"
            )
            days = balance.days_left
            cycle = (
                f"{days} {'day' if days == 1 else 'days'} left in billing cycle"
                if days is not None
                else "billing cycle days unavailable"
            )
            print(
                f"{balance.line}: {balance.remaining} remaining / "
                f"{balance.allowance} ({percentage}); {cycle}"
            )
