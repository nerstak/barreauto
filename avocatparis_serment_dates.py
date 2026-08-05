#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests>=2.32.0",
# ]
# ///

from __future__ import annotations

import argparse
import json
import os
import random
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


ENTRYPOINT_URL = "https://ssl.avocatparis.org/eInscription/Accueil.aspx"
DATES_PAGE_URL = "https://ssl.avocatparis.org/eInscription/PaiementEtDateSerment.aspx"
DATES_ENDPOINT_URL = f"{DATES_PAGE_URL}/ListerDatesSerment"
NTFY_BASE_URL = (os.getenv("NTFY_BASE_URL") or "https://ntfy.sh").rstrip("/")
DEFAULT_TIMEOUT = 30
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko; compatible; Contentpass/1.0) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Android 13; Mobile; rv:148.0) Gecko/148.0 Firefox/148.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/603.1.30 (KHTML, like Gecko) Version/26.5 Mobile/19E241 Safari/602.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/605.1",
]


@dataclass
class ParsedPage:
    title: str = ""
    forms: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    @property
    def login_form(self) -> dict[str, Any] | None:
        for form in self.forms:
            if form.get("id") == "formIdp" or form.get("name") == "formIdp":
                return form
        return None


class LoginPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.page = ParsedPage()
        self._in_title = False
        self._alert_depth = 0
        self._alert_parts: list[str] = []
        self._form_stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}

        if tag == "title":
            self._in_title = True
            return

        if tag == "form":
            self._form_stack.append(
                {
                    "id": attributes.get("id"),
                    "name": attributes.get("name"),
                    "action": attributes.get("action"),
                    "method": (attributes.get("method") or "get").lower(),
                    "inputs": {},
                }
            )
            return

        if tag == "div":
            css_classes = set(attributes.get("class", "").split())
            if "alert" in css_classes:
                self._alert_depth = 1
                self._alert_parts = []
            elif self._alert_depth:
                self._alert_depth += 1

        if self._form_stack and tag == "input":
            name = attributes.get("name")
            if name:
                self._form_stack[-1]["inputs"][name] = attributes.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return

        if tag == "form" and self._form_stack:
            self.page.forms.append(self._form_stack.pop())
            return

        if tag == "div" and self._alert_depth:
            self._alert_depth -= 1
            if self._alert_depth == 0:
                message = " ".join(part.strip() for part in self._alert_parts if part.strip())
                if message:
                    self.page.alerts.append(message)
                self._alert_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.page.title += data
        if self._alert_depth:
            self._alert_parts.append(data)


def parse_page(html: str) -> ParsedPage:
    parser = LoginPageParser()
    parser.feed(html)
    parser.close()
    parser.page.title = " ".join(parser.page.title.split())
    parser.page.alerts = [" ".join(alert.split()) for alert in parser.page.alerts]
    return parser.page


def build_session() -> requests.Session:
    session = requests.Session()
    user_agent = random.choice(USER_AGENTS)
    session.headers.update(
        {
            "User-Agent": user_agent
        }
    )
    session.user_agent = user_agent  # type: ignore[attr-defined]
    return session


def choose_post_url(login_page_url: str, form_action: str | None) -> str:
    if not form_action or form_action in {"#", "?"}:
        return login_page_url
    return urljoin(login_page_url, form_action)


def is_saml_post_back(page: ParsedPage) -> bool:
    if page.title.lower() != "post data":
        return False
    return any("SAMLResponse" in form["inputs"] for form in page.forms)


def follow_intermediate_forms(
    session: requests.Session,
    response: requests.Response,
    *,
    timeout: int,
) -> tuple[requests.Response, list[dict[str, Any]]]:
    extra_steps: list[dict[str, Any]] = []

    for _ in range(5):
        page = parse_page(response.text)
        if not is_saml_post_back(page):
            return response, extra_steps

        saml_form = next(form for form in page.forms if "SAMLResponse" in form["inputs"])
        action_url = choose_post_url(response.url, saml_form.get("action"))
        method = saml_form.get("method", "post").lower()
        if method != "post":
            raise RuntimeError(f"Unsupported intermediate form method: {method}")

        response = session.post(
            action_url,
            data=saml_form["inputs"],
            allow_redirects=True,
            timeout=timeout,
        )
        response.raise_for_status()
        extra_steps.append(
            {
                "kind": "saml_post_back",
                "url": action_url,
                "status_code": response.status_code,
            }
        )

    raise RuntimeError("Too many intermediate SAML form submissions.")


def serialize_cookie(cookie: Any) -> dict[str, Any]:
    return {
        "domain": cookie.domain,
        "name": cookie.name,
        "value": cookie.value,
        "path": cookie.path,
        "secure": cookie.secure,
        "expires": cookie.expires,
        "http_only": bool(cookie._rest.get("HttpOnly")),
    }


def session_cookie_candidates(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers = ("session", "saml", "auth", "asp.net", "fedauth")
    return [
        cookie
        for cookie in cookies
        if any(marker in cookie["name"].lower() for marker in markers)
    ]


def first_error_message(alerts: list[str]) -> str | None:
    for alert in alerts:
        if alert:
            return alert
    return None


def looks_successful(final_url: str, alerts: list[str]) -> bool:
    host = urlparse(final_url).netloc.lower()
    if host != "ssl.avocatparis.org":
        return False
    if any("accès refusé" in alert.lower() or "acces refuse" in alert.lower() for alert in alerts):
        return False
    return True


def format_bool(value: bool) -> str:
    return "oui" if value else "non"


def format_date(date_value: str) -> str:
    return date_value.split("T", 1)[0]


def format_slots(item: dict[str, Any]) -> str:
    return f"{item['places_libres']}/{item['places_totales']}"


def render_dates_table(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Aucune date disponible."

    headers = [
        "date",
        "ouvert",
        "selectionnable",
        "places libres",
        "places totales",
        "occupation %",
    ]
    rows: list[list[str]] = []
    for item in items:
        places = int(item["Places"])
        reservations = int(item["Reservations"])
        rows.append(
            [
                format_date(str(item["Date"])),
                format_bool(bool(item["Ouvert"])),
                format_bool(not bool(item["NonSelectionnable"])),
                str(places - reservations),
                str(places),
                f"{float(item['PourcentageOccupation']):.1f}",
            ]
        )

    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render_row(row: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    separator = "-+-".join("-" * width for width in widths)
    lines = [render_row(headers), separator]
    lines.extend(render_row(row) for row in rows)
    return "\n".join(lines)


def build_all_dates_message(dates_result: dict[str, Any]) -> str:
    available_items = [
        item for item in dates_result["items"] if item["occupation_pourcent"] < 100.0
    ]
    unavailable_items = [
        item for item in dates_result["items"] if item["occupation_pourcent"] >= 100.0
    ]

    lines: list[str] = []
    if available_items:
        lines.append("Dates disponibles")
        lines.extend(
            f"- {item['date']} ({format_slots(item)}) - {item['occupation_pourcent']:.1f}%"
            for item in available_items
        )
        lines.append("")

    lines.append("Dates indisponibles")
    if unavailable_items:
        lines.extend(
            f"- {item['date']} ({format_slots(item)}) - {item['occupation_pourcent']:.1f}%"
            for item in unavailable_items
        )
    else:
        lines.append("- Aucune")

    return "\n".join(lines)


def build_free_dates_message(items: list[dict[str, Any]]) -> str:
    lines = ["Dates avec disponibilité"]
    for item in items:
        lines.append(
            f"- {item['date']} ({format_slots(item)}) - {item['occupation_pourcent']:.1f}%"
        )
    return "\n".join(lines)


def publish_ntfy(
    channel: str,
    message: str,
    *,
    title: str,
    tags: str,
    priority: str,
    timeout: int,
) -> dict[str, Any]:
    try:
        response = requests.post(
            f"{NTFY_BASE_URL}/{channel}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Tags": tags,
                "Priority": priority,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return {
            "ok": True,
            "channel": channel,
            "title": title,
            "priority": priority,
            "status_code": response.status_code,
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "channel": channel,
            "title": title,
            "priority": priority,
            "error": str(exc),
        }


def send_success_notifications(
    dates_result: dict[str, Any],
    *,
    ntfy_all_channel: str | None,
    ntfy_free_channel: str | None,
    timeout: int,
) -> list[dict[str, Any]]:
    notifications: list[dict[str, Any]] = []

    if ntfy_all_channel:
        notifications.append(
            publish_ntfy(
                ntfy_all_channel,
                build_all_dates_message(dates_result),
                title="Run report",
                tags="information_source",
                priority="min",
                timeout=timeout,
            )
        )

    free_items = [
        item for item in dates_result["items"] if item["occupation_pourcent"] < 100.0
    ]
    if ntfy_free_channel and free_items:
        notifications.append(
            publish_ntfy(
                ntfy_free_channel,
                build_free_dates_message(free_items),
                title="Place disponible!",
                tags="warning",
                priority="max",
                timeout=timeout,
            )
        )

    return notifications


def send_error_notification(
    channel: str | None,
    *,
    stage: str,
    message: str,
    timeout: int,
) -> list[dict[str, Any]]:
    if not channel:
        return []
    return [
        publish_ntfy(
            channel,
            f"{stage}: {message}",
            title="Erreur script serment",
            tags="warning,error",
            priority="high",
            timeout=timeout,
        )
    ]


def summarize_notification_failures(notifications: list[dict[str, Any]]) -> str | None:
    failures = [notification for notification in notifications if not notification.get("ok", False)]
    if not failures:
        return None
    return "; ".join(f"{failure['channel']}: {failure['error']}" for failure in failures)


def fetch_dates(session: requests.Session, *, timeout: int) -> dict[str, Any]:
    page_response = session.get(DATES_PAGE_URL, allow_redirects=True, timeout=timeout)
    page_response.raise_for_status()
    if urlparse(page_response.url).netloc.lower() != "ssl.avocatparis.org":
        raise RuntimeError("La session n'a pas accès à la page des dates.")

    response = session.post(
        DATES_ENDPOINT_URL,
        data="",
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://ssl.avocatparis.org",
            "Referer": DATES_PAGE_URL,
        },
        timeout=timeout,
    )
    response.raise_for_status()

    payload = response.json()
    encoded_result = payload.get("d")
    if not isinstance(encoded_result, str):
        raise RuntimeError("Unexpected dates payload: missing string field 'd'.")

    decoded_result = json.loads(encoded_result)
    if not isinstance(decoded_result, list) or len(decoded_result) != 2:
        raise RuntimeError("Unexpected dates payload structure.")

    status, items = decoded_result
    if not isinstance(status, dict):
        raise RuntimeError("Unexpected dates status structure.")
    if not isinstance(items, list):
        raise RuntimeError("Unexpected dates list structure.")

    code_erreur = int(status.get("CodeErreur", 0))
    message_erreur = status.get("MessageErreur")
    if code_erreur != 0 or message_erreur:
        raise RuntimeError(
            f"Erreur récupération des dates: code={code_erreur}, message={message_erreur or 'inconnu'}"
        )

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        places = int(item["Places"])
        reservations = int(item["Reservations"])
        normalized_items.append(
            {
                "date": format_date(str(item["Date"])),
                "ouvert": bool(item["Ouvert"]),
                "selectionnable": not bool(item["NonSelectionnable"]),
                "places_libres": places - reservations,
                "places_totales": places,
                "reservations": reservations,
                "occupation_pourcent": float(item["PourcentageOccupation"]),
                "audiences": str(item["Audiences"]),
                "raw": item,
            }
        )

    return {
        "count": len(normalized_items),
        "items": normalized_items,
        "raw_status": status,
        "raw_items": items,
        "table": render_dates_table(items),
    }


def run_login(
    username: str,
    password: str,
    *,
    totp: str | None,
    timeout: int,
) -> tuple[requests.Session, dict[str, Any], int]:
    session = build_session()

    login_page_response = session.get(ENTRYPOINT_URL, allow_redirects=True, timeout=timeout)
    login_page_response.raise_for_status()
    login_page = parse_page(login_page_response.text)

    login_form = login_page.login_form
    if not login_form:
        raise RuntimeError("Unable to find the login form on the login page.")

    auth_state = login_form["inputs"].get("AuthState")
    if not auth_state:
        raise RuntimeError("Unable to find AuthState on the login page.")

    auth_method = "TOTP" if totp else "LOGIN"
    post_url = choose_post_url(login_page_response.url, login_form.get("action"))
    payload = {
        **login_form["inputs"],
        "profession": login_form["inputs"].get("profession", "AVOCAT"),
        "authmethod": auth_method,
        "username": username.strip(),
        "password": password.strip(),
        "totp": (totp or "").strip(),
    }

    final_response = session.post(post_url, data=payload, allow_redirects=True, timeout=timeout)
    final_response.raise_for_status()
    final_response, intermediate_steps = follow_intermediate_forms(
        session,
        final_response,
        timeout=timeout,
    )
    final_page = parse_page(final_response.text)

    cookies = [serialize_cookie(cookie) for cookie in session.cookies]
    result = {
        "success": looks_successful(final_response.url, final_page.alerts),
        "user_agent": session.headers["User-Agent"],
        "auth_method": auth_method,
        "entrypoint_url": ENTRYPOINT_URL,
        "login_page_url": login_page_response.url,
        "post_url": post_url,
        "final_url": final_response.url,
        "final_title": final_page.title,
        "redirects": [
            {
                "status_code": response.status_code,
                "url": response.url,
                "location": response.headers.get("Location"),
            }
            for response in final_response.history
        ],
        "intermediate_steps": intermediate_steps,
        "cookies": cookies,
        "session_candidates": session_cookie_candidates(cookies),
        "error_message": first_error_message(final_page.alerts),
    }

    exit_code = 0 if result["success"] else 1
    return session, result, exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log in to ssl.avocatparis.org, fetch serment dates, and print availability."
    )
    parser.add_argument("--username", default=os.getenv("EMAIL"))
    parser.add_argument("--password", default=os.getenv("PASSWORD"))
    parser.add_argument("--totp", default=os.getenv("AVOCATPARIS_TOTP"))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--ntfy-all-channel",
        default=os.getenv("NTFY_ALL_CHANNEL"),
        help="ntfy.sh channel receiving errors and the full dates table.",
    )
    parser.add_argument(
        "--ntfy-free-channel",
        default=os.getenv("NTFY_FREE_CHANNEL"),
        help="ntfy.sh channel receiving only dates with free capacity.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path. Stdout is always written.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full JSON result instead of the human-readable summary.",
    )
    args = parser.parse_args()

    if not args.username:
        parser.error("Missing username. Pass --username or set EMAIL.")
    if not args.password:
        parser.error("Missing password. Pass --password or set PASSWORD.")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0.")

    return args


def main() -> int:
    args = parse_args()

    try:
        session, login_result, exit_code = run_login(
            args.username,
            args.password,
            totp=args.totp,
            timeout=args.timeout,
        )
        if exit_code != 0:
            message = login_result.get("error_message") or "Erreur de connexion."
            notifications = send_error_notification(
                args.ntfy_all_channel,
                stage="Erreur de connexion",
                message=message,
                timeout=args.timeout,
            )
            output = {
                "success": False,
                "stage": "login",
                "error": message,
                "login": login_result,
                "notifications": notifications,
            }
            rendered = json.dumps(output, ensure_ascii=False, indent=2)
            notification_error = summarize_notification_failures(notifications)
            if args.json:
                print(rendered)
            else:
                suffix = f" | notification ntfy échouée: {notification_error}" if notification_error else ""
                print(f"Erreur de connexion: {message}{suffix}")
            if args.output:
                args.output.write_text(rendered + "\n", encoding="utf-8")
            return exit_code

        dates_result = fetch_dates(session, timeout=args.timeout)
        notifications = send_success_notifications(
            dates_result,
            ntfy_all_channel=args.ntfy_all_channel,
            ntfy_free_channel=args.ntfy_free_channel,
            timeout=args.timeout,
        )
        output = {
            "success": True,
            "stage": "dates",
            "login": login_result,
            "dates": dates_result,
            "notifications": notifications,
        }
        rendered = json.dumps(output, ensure_ascii=False, indent=2)
        notification_error = summarize_notification_failures(notifications)

        if args.json:
            print(rendered)
        else:
            print(dates_result["table"])
            if notification_error:
                print(f"Notification ntfy échouée: {notification_error}")

        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")

        return 2 if notification_error else 0
    except requests.RequestException as exc:
        notifications = send_error_notification(
            args.ntfy_all_channel,
            stage="Erreur réseau",
            message=str(exc),
            timeout=args.timeout,
        )
        output = {"success": False, "error": str(exc)}
        if notifications:
            output["notifications"] = notifications
        rendered = json.dumps(output, ensure_ascii=False, indent=2)
        notification_error = summarize_notification_failures(notifications)
        if args.json:
            print(rendered)
        else:
            suffix = f" | notification ntfy échouée: {notification_error}" if notification_error else ""
            print(f"Erreur réseau: {exc}{suffix}")
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        return 2
    except Exception as exc:
        notifications = send_error_notification(
            args.ntfy_all_channel,
            stage="Erreur script",
            message=str(exc),
            timeout=args.timeout,
        )
        output = {"success": False, "error": str(exc)}
        if notifications:
            output["notifications"] = notifications
        rendered = json.dumps(output, ensure_ascii=False, indent=2)
        notification_error = summarize_notification_failures(notifications)
        if args.json:
            print(rendered)
        else:
            suffix = f" | notification ntfy échouée: {notification_error}" if notification_error else ""
            print(f"Erreur: {exc}{suffix}")
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
