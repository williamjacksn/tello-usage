# tello-usage

tello-usage is a Python package and command-line tool that retrieves data balances for every line in a Tello Mobile
account. It reports remaining data, percentage available, renewal dates, and days remaining in each billing cycle.

The package uses httpx2 and Beautiful Soup to access Tello Mobile's account pages. It does not require a browser or
browser automation software.

This is an independent, unofficial package. It is not developed by, affiliated with, or endorsed by Tello Mobile.

## Requirements

The package requires Python 3.11 or later and a Tello Mobile account with an email address and password.

## Installation

Install the command-line tool with uv:

```sh
uv tool install tello-usage
```

To use the package in a Python project, add it as a dependency:

```sh
uv add tello-usage
```

The distribution is named `tello-usage`. The Python import is named `tello_usage`,
and the command-line executable is named `tello-usage`.

## Command-line usage

Run the following command to retrieve balances:

```sh
tello-usage
```

The command prompts for your Tello Mobile email address and password. The password is hidden while you type.

Each output line includes the account's line label, remaining data, percentage available, and days left in the billing
cycle:

```text
(555) 010-0100 - Example: 457.59 MB remaining / 10 GB (4.58% available); 4 days left in billing cycle
```

Use `--json` to produce JSON output:

```sh
tello-usage --json
```

The output contains one object for each line:

```json
[
  {
    "subscription_id": "12345678",
    "line": "(555) 010-0100 - Example",
    "remaining": "457.59 MB",
    "allowance": "10 GB",
    "renewal_date": "2026-10-23",
    "percentage_available": 4.58,
    "days_left": 4
  }
]
```

This example assumes the command runs on October 19, 2026.

If you installed the package as a project dependency, run the command through uv:

```sh
uv run tello-usage --json
```

### Credentials

You can supply your email address with `--username`:

```sh
tello-usage --username you@example.com
```

The command also accepts the following environment variables:

- `TELLO_USERNAME` supplies the account email address.
- `TELLO_PASSWORD` supplies the account password.

The `--username` argument takes precedence over `TELLO_USERNAME`. Missing credentials are requested through terminal
prompts.

For unattended use, supply both credentials through your process environment. You can also provide the email address
with `--username`. The command exits with an error if credentials are missing and a terminal prompt is unavailable.

There is no password command-line argument. The package does not load `.env` files automatically.

### Reusing sessions

Use `--cookie-file` to save the login session and reuse it on later runs:

```sh
tello-usage --cookie-file ~/.cache/tello-usage/cookies.json
```

You can also set the `TELLO_COOKIE_FILE` environment variable. The `--cookie-file` argument takes precedence. Cookies are
saved only when you choose a file.

If the saved session is still valid, the command skips the login and does not ask for a password. If the session has
expired or belongs to a different `--username`, the command uses the usual credentials, logs in, and replaces the file.
The file is written only after balances are retrieved successfully. If it cannot be written, the command prints a
warning and still shows the balances.

The cookie file can be used to access your Tello Mobile account. Protect it like a password. Where the operating system
supports it, the file is readable only by the user who created it.

## Python usage

Use `TelloClient` in a `with` statement so its HTTP connections are closed after use:

```python
import os

from tello_usage import TelloClient

with TelloClient() as client:
    balances = client.balances(
        os.environ["TELLO_USERNAME"],
        os.environ["TELLO_PASSWORD"],
    )

for balance in balances:
    print(
        balance.line,
        balance.remaining,
        balance.percentage_available,
        balance.renewal_date,
        balance.days_left,
    )
```

The synchronous `balances(username, password)` method logs in and returns a list of `Balance` objects.

The default HTTP timeout is 30 seconds. You can override it when creating the client:

```python
with TelloClient(timeout=60) as client:
    balances = client.balances(username, password)
```

### Reusing sessions

Pass `cookie_file` to load a saved session and save it again after each successful call:

```python
from tello_usage import TelloClient, TelloLoginRequired

with TelloClient(cookie_file="~/.cache/tello-usage/cookies.json") as client:
    try:
        balances = client.balances()  # Reuses the saved session.
    except TelloLoginRequired:
        balances = client.balances(username, password)  # Logs in and saves.
```

The username and password are optional for `balances()`:

- If a logged-in session exists and belongs to `username`, the client reuses it. When no username is given, any saved
  session is reused. Usernames are compared without regard to case.
- Otherwise, the client logs in with the credentials. An expired session is discarded first.
- If a login is needed and the credentials are missing, the client raises `TelloLoginRequired`, a subclass of
  `TelloError`.

Passing credentials every time also works. The client then logs in only when the saved session is no longer valid.

If the cookie file cannot be written, `balances()` issues a `RuntimeWarning` and still returns the balances. A missing,
unreadable, or unrecognized cookie file is ignored.

To store sessions somewhere else, call the methods directly:

- `load_cookies(path)` replaces the client's cookies with a saved session. It returns `False` if the file cannot be used.
- `save_cookies(path)` saves the current logged-in session. It raises `TelloLoginRequired` if there is no session. The
  file is written atomically, and only Tello Mobile cookies and the account username are saved.

### Balance values

Each `Balance` object provides the following values. JSON output uses the same names.

- `subscription_id` is Tello Mobile's identifier for the line.
- `line` is the label displayed in the account's line selector.
- `remaining` is the displayed remaining data amount, including its unit.
- `allowance` is the displayed data allowance, including its unit.
- `renewal_date` is the data plan's renewal or expiration date in `YYYY-MM-DD` format.
- `percentage_available` is the remaining data expressed as a percentage of the displayed allowance.
- `days_left` is the number of calendar days until the renewal date.

The percentage calculation uses the displayed amounts, assumes 1 GB equals 1,000 MB, and rounds the result to two
decimal places. The result is limited to 0–100 percent. A rollover balance above the allowance therefore reports 100
percent available.

An unlimited or zero allowance produces `None` for `percentage_available`.

Days remaining are calculated using the computer's local date. A renewal date of today or a date in the past produces
zero days remaining. This value does not measure the exact number of hours until renewal.

If the account page does not provide a recognized renewal date, `renewal_date` and `days_left` are `None`. Python `None`
values appear as `null` in JSON.

## Error handling

The client raises `TelloError` when login fails, required account information is missing, a line cannot be selected, or
a data balance cannot be read. Network and HTTP errors are raised as httpx2 exceptions.

```python
import os

import httpx2
from tello_usage import TelloClient, TelloError

try:
    with TelloClient() as client:
        balances = client.balances(
            os.environ["TELLO_USERNAME"],
            os.environ["TELLO_PASSWORD"],
        )
except TelloError as error:
    print(f"Could not read account balances: {error}")
except httpx2.HTTPError as error:
    print(f"The HTTP request failed: {error}")
```

The command-line tool writes errors to standard error and exits with a nonzero status.

A missing data balance stops the operation without returning partial results. This can occur when a line has no active
data plan.

## Website access and limitations

The package accesses Tello Mobile's website forms rather than a documented API. It logs in, discovers the account's
lines, and selects each line to read its balance. It maintains session cookies and reads fresh form tokens between
requests.

The client changes the selected account line while retrieving balances. It does not purchase data or change plan
settings.

Changes to Tello Mobile's website, a CAPTCHA, or an additional account verification step may prevent the client from
working.

Credentials are held in memory and are never saved by the package. Session cookies are saved only when you choose a
cookie file, as described in [Reusing sessions](#reusing-sessions).

## Development

From a source checkout, install dependencies and run the tests with uv:

```sh
uv sync
uv run python -m unittest discover -s tests -v
```

The tests use generated account pages and simulated HTTP responses. They do not require account credentials or make
requests to Tello Mobile.

## License

This project is licensed under the [MIT License](LICENSE).
