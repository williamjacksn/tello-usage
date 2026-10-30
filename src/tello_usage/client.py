import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.metadata import version
from types import TracebackType
from typing import Self
from urllib.parse import urljoin, urlsplit

import certifi
import httpx2
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://tello.com"
DATA_AMOUNT = r"\d[\d,]*(?:\.\d+)?\s*(?:KB|MB|GB|TB)"
DATA_BALANCE = re.compile(
    rf"(?P<remaining>{DATA_AMOUNT})\s+remaining\s*/\s*"
    rf"(?P<allowance>{DATA_AMOUNT}|Unlimited(?:\s+data)?)",
    re.IGNORECASE,
)


class TelloError(Exception):
    """Authentication failed or Tello Mobile returned an unexpected account page."""


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


def _account_page(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('#account_login_form, input[name="password"]'):
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


class TelloClient:
    """A fresh, in-memory cookie session; close it after fetching balances."""

    def __init__(
        self,
        *,
        timeout: float = 30,
        transport: httpx2.BaseTransport | None = None,
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

    def balances(self, username: str, password: str) -> list[Balance]:
        """Log in, enumerate every line and read each line's data balance."""
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
        return [results[subscription_id] for subscription_id in lines]
