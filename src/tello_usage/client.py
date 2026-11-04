import json
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from http.cookiejar import Cookie
from importlib.metadata import version
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin, urlsplit

import certifi
import httpx2
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://tello.com"
COOKIE_FILE_VERSION = 1
DATA_AMOUNT = r"\d[\d,]*(?:\.\d+)?\s*(?:KB|MB|GB|TB)"
DATA_BALANCE = re.compile(
    rf"(?P<remaining>{DATA_AMOUNT})\s+remaining\s*/\s*"
    rf"(?P<allowance>{DATA_AMOUNT}|Unlimited(?:\s+data)?)",
    re.IGNORECASE,
)


class TelloError(Exception):
    """Authentication failed or Tello Mobile returned an unexpected account page."""


class TelloLoginRequired(TelloError):
    """No reusable session is available and no credentials were supplied."""


@dataclass(frozen=True)
class Balance:
    subscription_id: str
    line: str
    remaining: str
    allowance: str
    renewal_date: str | None = None

    @property
    def days_left(self) -> int | None:
        """Calendar days until renewal, using the computer's local date."""
        if self.renewal_date is None:
            return None
        # Billing cycles use the local calendar date, not a timestamp.
        today = date.today()  # noqa: DTZ011
        return max(0, (date.fromisoformat(self.renewal_date) - today).days)

    @property
    def percentage_available(self) -> float | None:
        """Estimate available data from displayed balances, using 1 GB = 1,000 MB."""

        def amount(value: str) -> Decimal | None:
            match = re.fullmatch(r"([\d,.]+)\s*(KB|MB|GB|TB)", value, re.IGNORECASE)
            if match is None:
                return None
            unit = match[2].upper()
            return Decimal(match[1].replace(",", "")) * 1000 ** (
                "KB",
                "MB",
                "GB",
                "TB",
            ).index(unit)

        remaining = amount(self.remaining)
        allowance = amount(self.allowance)
        if remaining is None or allowance is None or allowance <= 0:
            return None
        available = remaining / allowance * 100
        return float(
            max(Decimal(0), min(Decimal(100), available)).quantize(Decimal("0.01"))
        )


def _form_fields(form: Tag) -> dict[str, str]:
    """Include fresh CSRF, snowman and dynamically named honeypot fields."""
    return {
        str(field["name"]): str(field.get("value", ""))
        for field in form.select("input[name]")
        if not field.has_attr("disabled")
        and field.get("type", "text") not in {"submit", "button", "checkbox", "radio"}
    }


def _form(soup: BeautifulSoup, selector: str, action: str) -> Tag:
    form = soup.select_one(selector)
    if form is None:
        raise TelloError(
            "Expected Tello Mobile form is missing. Login may require verification, "
            "or the website has changed."
        )
    if (
        urljoin(BASE_URL, str(form.get("action", ""))) != BASE_URL + action
        or str(form.get("method", "get")).lower() != "post"
    ):
        raise TelloError("Tello Mobile returned an unexpected form action or method.")
    if not _form_fields(form).get("_token"):
        raise TelloError("Tello Mobile's form is missing its CSRF token.")
    return form


def _is_login_page(soup: BeautifulSoup) -> bool:
    return soup.select_one('#account_login_form, input[name="password"]') is not None


def _account_page(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    if _is_login_page(soup):
        raise TelloError(
            "Tello Mobile login failed or the session expired. Check your credentials "
            "and whether Tello Mobile requires account verification."
        )
    return soup


def _lines(soup: BeautifulSoup) -> tuple[dict[str, str], str, Tag]:
    selector = soup.select_one('select[name="selected_subscription_id"]')
    if selector is None:
        raise TelloError(
            "Tello Mobile's account line selector is missing; cannot enumerate lines."
        )
    form = _form(
        soup,
        'form:has(select[name="selected_subscription_id"])',
        "/account/process_home",
    )
    lines = {
        str(option["value"]): option.get_text(" ", strip=True)
        for option in selector.select("option[value]")
        if option["value"] and not option.has_attr("disabled")
    }
    selected = selector.select_one("option[selected]")
    if selected is None:
        selected = selector.select_one("option[value]")
    current = str(selected.get("value", "")) if selected else ""
    if not lines or current not in lines:
        raise TelloError("Tello Mobile returned no selectable account lines.")
    if _form_fields(form).get("action") != "change_selected_subscription":
        raise TelloError("Tello Mobile returned an unexpected line-selection action.")
    return lines, current, form


def _renewal_date(detail: Tag) -> str | None:
    # Use the date beside the data balance, not voice/text or add-on dates.
    holder = detail.find_parent(class_="progress_holder")
    if holder is None:
        return None
    for node in holder.select(".valid-date"):
        match = re.fullmatch(
            r"(?:Renewal|Expiration|Expiry) date:\s*(\d{1,2}/\d{1,2}/\d{4})",
            node.get_text(" ", strip=True),
            re.IGNORECASE,
        )
        if match:
            try:
                month, day, year = map(int, match[1].split("/"))
                return date(year, month, day).isoformat()
            except ValueError:
                return None
    return None


def _balance(soup: BeautifulSoup, subscription_id: str, label: str) -> Balance:
    # Restrict parsing to balance blocks, excluding plan offers and add-ons.
    for detail in soup.select(".balance-details .balance-from"):
        match = DATA_BALANCE.fullmatch(detail.get_text(" ", strip=True))
        if match:
            return Balance(
                subscription_id,
                label,
                match["remaining"],
                match["allowance"],
                _renewal_date(detail),
            )
    raise TelloError(
        "Could not find a remaining data balance for an account line. The line may "
        "have no active data plan, or Tello Mobile's page format has changed."
    )


def _same_user(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def _is_tello_cookie(cookie: Cookie) -> bool:
    domain = cookie.domain.lstrip(".").lower()
    return domain == "tello.com" or domain.endswith(".tello.com")


def _cookie_to_dict(cookie: Cookie) -> dict[str, Any]:
    return {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "domain_specified": cookie.domain_specified,
        "domain_initial_dot": cookie.domain_initial_dot,
        "path": cookie.path,
        "path_specified": cookie.path_specified,
        "secure": cookie.secure,
        "expires": cookie.expires,
        "discard": cookie.discard,
        "http_only": cookie.has_nonstandard_attr("HttpOnly"),
    }


def _cookie_from_dict(data: dict[str, Any]) -> Cookie:
    expires = data["expires"]
    if not isinstance(data["name"], str) or not isinstance(data["domain"], str):
        raise TypeError("invalid cookie")
    if expires is not None and not isinstance(expires, int):
        raise TypeError("invalid cookie expiration")
    return Cookie(
        version=0,
        name=data["name"],
        value=data["value"] if isinstance(data["value"], str) else None,
        port=None,
        port_specified=False,
        domain=data["domain"],
        domain_specified=bool(data["domain_specified"]),
        domain_initial_dot=bool(data["domain_initial_dot"]),
        path=str(data["path"]),
        path_specified=bool(data["path_specified"]),
        secure=bool(data["secure"]),
        expires=expires,
        discard=bool(data["discard"]),
        comment=None,
        comment_url=None,
        rest={"HttpOnly": ""} if data.get("http_only") else {},
    )


class TelloClient:
    """An HTTP session for Tello Mobile; close it after fetching balances.

    Cookies are held in memory unless ``cookie_file`` is given. When it is, a saved
    session is loaded from that file and the file is rewritten after balances are
    retrieved successfully, so later runs can skip logging in.
    """

    def __init__(
        self,
        *,
        timeout: float = 30,
        transport: httpx2.BaseTransport | None = None,
        cookie_file: str | os.PathLike[str] | None = None,
    ) -> None:
        _version = version("tello-usage")
        self._client = httpx2.Client(
            base_url=BASE_URL,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": f"tello-usage/{_version}"},
            event_hooks={"request": [self._check_origin]},
            transport=transport,
            verify=certifi.where(),
        )
        self._session_username: str | None = None
        self._cookie_file = Path(cookie_file).expanduser() if cookie_file else None
        if self._cookie_file is not None:
            self.load_cookies(self._cookie_file)

    def load_cookies(self, path: str | os.PathLike[str]) -> bool:
        """Load a saved session, replacing current cookies.

        Returns ``False``, leaving the session empty, when the file is missing,
        unreadable or not a recognized cookie file.
        """
        self._clear_session()
        try:
            data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
            if data["version"] != COOKIE_FILE_VERSION:
                return False
            username = data["username"]
            if not isinstance(username, str) or not username:
                return False
            cookies = [_cookie_from_dict(cookie) for cookie in data["cookies"]]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        for cookie in cookies:
            if _is_tello_cookie(cookie) and not cookie.is_expired():
                self._client.cookies.jar.set_cookie(cookie)
        if not len(self._client.cookies.jar):
            return False
        self._session_username = username
        return True

    def save_cookies(self, path: str | os.PathLike[str]) -> None:
        """Save the current logged-in session so a later client can reuse it.

        The file is readable only by the current user where the platform supports
        it. Anyone who can read the file can access the Tello Mobile account.
        """
        if self._session_username is None:
            raise TelloLoginRequired("There is no logged-in session to save.")
        target = Path(path).expanduser()
        data = {
            "version": COOKIE_FILE_VERSION,
            "username": self._session_username,
            "cookies": [
                _cookie_to_dict(cookie)
                for cookie in self._client.cookies.jar
                if _is_tello_cookie(cookie)
            ],
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp creates the file with owner-only permissions.
        fd, temporary = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _clear_session(self) -> None:
        self._client.cookies.clear()
        self._session_username = None

    @staticmethod
    def _check_origin(request: httpx2.Request) -> None:
        url = urlsplit(str(request.url))
        if (url.scheme, url.hostname, url.port) != ("https", "tello.com", None):
            raise TelloError(
                "Tello Mobile redirected outside the expected secure account website."
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        response = self._client.request(method, path, data=data, headers=headers)
        response.raise_for_status()
        return response.text

    def _resume(self, username: str | None) -> BeautifulSoup | None:
        """Return the account page if the current session is still logged in."""
        if self._session_username is None or not len(self._client.cookies.jar):
            return None
        if username is not None and not _same_user(username, self._session_username):
            return None
        soup = BeautifulSoup(self._request("GET", "/account/home"), "html.parser")
        if _is_login_page(soup):
            # Expired: discard it so a retry with credentials logs in directly.
            self._clear_session()
            return None
        return soup

    def _login(self, username: str, password: str) -> BeautifulSoup:
        self._clear_session()
        login = BeautifulSoup(self._request("GET", "/account/login"), "html.parser")
        form = _form(login, "#account_login_form", "/account/process_login")
        fields = _form_fields(form)
        fields.update(username=username, password=password, return_to="/account/home")
        soup = _account_page(
            self._request(
                "POST",
                "/account/process_login",
                data=fields,
                headers={"Origin": BASE_URL, "Referer": BASE_URL + "/account/login"},
            )
        )
        self._session_username = username
        return soup

    def balances(
        self, username: str | None = None, password: str | None = None
    ) -> list[Balance]:
        """Enumerate every line and read each line's data balance.

        A logged-in session (loaded from a cookie file or left by an earlier call)
        is reused when it belongs to ``username`` or no username is given.
        Otherwise the client logs in, raising ``TelloLoginRequired`` if
        credentials are missing.
        """
        soup = self._resume(username)
        if soup is None:
            if not username or not password:
                raise TelloLoginRequired(
                    "Tello Mobile login is required, but no saved session is "
                    "available and no username and password were supplied."
                )
            soup = self._login(username, password)
        lines, current, _ = _lines(soup)
        results = {current: _balance(soup, current, lines[current])}
        for subscription_id, label in lines.items():
            if subscription_id in results:
                continue
            # Tokens and honeypot field names rotate after every line switch.
            _, _, switch_form = _lines(soup)
            fields = _form_fields(switch_form)
            fields["selected_subscription_id"] = subscription_id
            soup = _account_page(
                self._request(
                    "POST",
                    "/account/process_home",
                    data=fields,
                    headers={"Origin": BASE_URL, "Referer": BASE_URL + "/account/home"},
                )
            )
            _, selected, _ = _lines(soup)
            if selected != subscription_id:
                raise TelloError(
                    "Tello Mobile did not switch to the requested line; "
                    "stopping to avoid mislabeling balances."
                )
            results[subscription_id] = _balance(soup, subscription_id, label)
        if self._cookie_file is not None:
            try:
                self.save_cookies(self._cookie_file)
            except OSError as error:
                warnings.warn(
                    f"Could not save Tello Mobile cookies to {self._cookie_file}: "
                    f"{error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        return [results[subscription_id] for subscription_id in lines]
