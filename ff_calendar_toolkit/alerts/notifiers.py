import json
import os
import time
from urllib import parse, request
from urllib.error import HTTPError, URLError

from ff_calendar_toolkit.models import AlertConnector, AlertOptions

from .models import AlertEvent, AlertRule


class NotificationError(RuntimeError):
    pass


class NotifierFactory:
    def __init__(self, options: AlertOptions) -> None:
        self.options = options
        self.connector_map = {
            connector.connector_id: connector
            for connector in options.connectors
            if connector.enabled
        }

    def connector_ids(self) -> list[str]:
        return sorted(self.connector_map)

    def send_raw(
        self,
        connector_id: str,
        message: str,
    ) -> str | None:

        connector = self.connector_map.get(
            connector_id
        )

        if connector is None:
            raise NotificationError(
                f"Connector '{connector_id}' "
                f"is not configured or enabled"
            )

        if connector.connector_type == "discord":
            return self._send_discord(
                connector,
                message,
            )

        elif connector.connector_type == "telegram":
            self._send_telegram(
                connector,
                message,
            )

        elif connector.connector_type == "webhook":
            url = _required_env(
                connector.settings.get(
                    "url_env"
                )
            )

            headers = {}

            header_name = connector.settings.get(
                "auth_header_name"
            )

            header_env = connector.settings.get(
                "auth_header_env"
            )

            if header_name and header_env:
                headers[str(header_name)] = (
                    _required_env(
                        header_env
                    )
                )

            _post_json(
                url,
                {"message": message},
                headers=headers,
            )

        else:
            raise NotificationError(
                f"Unsupported connector type "
                f"'{connector.connector_type}'"
            )

        return None

    def send(
        self,
        connector_id: str,
        rule: AlertRule,
        event: AlertEvent,
    ) -> None:

        connector = self.connector_map.get(
            connector_id
        )

        if connector is None:
            raise NotificationError(
                f"Connector '{connector_id}' "
                f"is not configured or enabled"
            )

        message = render_message(
            self.options.message_prefix,
            rule,
            event,
        )

        for attempt in range(
            1,
            self.options.retry_attempts + 1,
        ):
            try:
                self._send_once(
                    connector,
                    message,
                    rule,
                    event,
                )
                return

            except NotificationError:
                if (
                    attempt
                    == self.options.retry_attempts
                ):
                    raise

                time.sleep(
                    self.options.retry_backoff_seconds
                    * attempt
                )

    def _send_once(
        self,
        connector: AlertConnector,
        message: str,
        rule: AlertRule,
        event: AlertEvent,
    ) -> None:

        if connector.connector_type == "discord":
            self._send_discord(
                connector,
                message,
            )
            return

        if connector.connector_type == "telegram":
            self._send_telegram(
                connector,
                message,
            )
            return

        if connector.connector_type == "webhook":
            self._send_webhook(
                connector,
                message,
                rule,
                event,
            )
            return

        raise NotificationError(
            f"Unsupported connector type "
            f"'{connector.connector_type}'"
        )

    def _send_discord(
        self,
        connector: AlertConnector,
        message: str,
    ) -> str:

        webhook_url = _required_env(
            connector.settings.get(
                "webhook_url_env"
            )
        )

        if "?" in webhook_url:
            send_url = (
                f"{webhook_url}&wait=true"
            )
        else:
            send_url = (
                f"{webhook_url}?wait=true"
            )

        response = _post_json(
            send_url,
            {"content": message},
        )

        if not response:
            raise NotificationError(
                "Discord did not return "
                "a message ID"
            )

        try:
            data = json.loads(response)

        except json.JSONDecodeError as exc:
            raise NotificationError(
                "Discord returned an "
                "invalid response"
            ) from exc

        message_id = data.get("id")

        if not message_id:
            raise NotificationError(
                "Discord response did not "
                "contain a message ID"
            )

        return str(message_id)

    def edit_discord_message(
        self,
        connector_id: str,
        message_id: str,
        message: str,
    ) -> None:

        connector = self.connector_map.get(
            connector_id
        )

        if connector is None:
            raise NotificationError(
                f"Connector '{connector_id}' "
                f"is not configured or enabled"
            )

        if connector.connector_type != "discord":
            raise NotificationError(
                f"Connector '{connector_id}' "
                f"is not a Discord connector"
            )

        webhook_url = _required_env(
            connector.settings.get(
                "webhook_url_env"
            )
        )

        edit_url = (
            f"{webhook_url}/messages/"
            f"{message_id}"
        )

        _patch_json(
            edit_url,
            {"content": message},
        )

    def delete_discord_message(
        self,
        connector_id: str,
        message_id: str,
    ) -> None:

        connector = self.connector_map.get(
            connector_id
        )

        if connector is None:
            raise NotificationError(
                f"Connector '{connector_id}' "
                f"is not configured or enabled"
            )

        if connector.connector_type != "discord":
            raise NotificationError(
                f"Connector '{connector_id}' "
                f"is not a Discord connector"
            )

        webhook_url = _required_env(
            connector.settings.get(
                "webhook_url_env"
            )
        )

        delete_url = (
            f"{webhook_url}/messages/"
            f"{message_id}"
        )

        _perform_delete_request(
            delete_url,
            {
                "User-Agent": (
                    "DiscordBot "
                    "(forex_factory_calendar_"
                    "news_scraper, 1.0)"
                )
            },
        )

    def _send_telegram(
        self,
        connector: AlertConnector,
        message: str,
    ) -> None:

        bot_token = _required_env(
            connector.settings.get(
                "bot_token_env"
            )
        )

        chat_id = _required_env(
            connector.settings.get(
                "chat_id_env"
            )
        )

        payload = parse.urlencode(
            {
                "chat_id": chat_id,
                "text": message,
            }
        ).encode("utf-8")

        url = (
            f"https://api.telegram.org/"
            f"bot{bot_token}/sendMessage"
        )

        _post_form(
            url,
            payload,
        )

    def _send_webhook(
        self,
        connector: AlertConnector,
        message: str,
        rule: AlertRule,
        event: AlertEvent,
    ) -> None:

        url = _required_env(
            connector.settings.get(
                "url_env"
            )
        )

        headers = {}

        header_name = connector.settings.get(
            "auth_header_name"
        )

        header_env = connector.settings.get(
            "auth_header_env"
        )

        if header_name and header_env:
            headers[str(header_name)] = (
                _required_env(
                    header_env
                )
            )

        payload = {
            "message": message,
            "rule": {
                "id": rule.rule_id,
                "name": rule.name,
            },
            "event": event.payload,
            "event_time": (
                event.event_time.isoformat()
            ),
        }

        _post_json(
            url,
            payload,
            headers=headers,
        )


def render_message(
    prefix: str,
    rule: AlertRule,
    event: AlertEvent,
) -> str:

    payload = event.payload

    currency = payload.get(
        "currency",
        "",
    )

    currency_flags = {
        "USD": "🇺🇸",
        "EUR": "🇪🇺",
        "GBP": "🇬🇧",
        "JPY": "🇯🇵",
        "NZD": "🇳🇿",
        "AUD": "🇦🇺",
    }

    impact = payload.get(
        "impact",
        "",
    )

    impact_icons = {
        "red": "🔴",
        "orange": "🟠",
    }

    flag = currency_flags.get(
        currency,
        "",
    )

    impact_icon = impact_icons.get(
        impact,
        "⚪",
    )

    event_name = payload.get(
        "event",
        "",
    )

    release_time = payload.get(
        "time",
        "",
    )

    forecast = str(
        payload.get(
            "forecast",
            "",
        )
        or ""
    ).strip()

    previous = str(
        payload.get(
            "previous",
            "",
        )
        or ""
    ).strip()

    lines = [
        "🚨 ECONOMIC NEWS ALERT",
        "",
        f"{flag} {currency}",
        f"{impact_icon} {event_name}",
        "",
        f"⏰ Release: {release_time} MYT",
        "⏳ 10 minutes remaining",
        "",
    ]

    if forecast:
        lines.append(
            f"Forecast: {forecast}"
        )

    if previous:
        lines.append(
            f"Previous: {previous}"
        )

    return "\n".join(lines)


def render_actual_message(
    event: AlertEvent,
) -> str:

    payload = event.payload

    currency = payload.get(
        "currency",
        "",
    )

    currency_flags = {
        "USD": "🇺🇸",
        "EUR": "🇪🇺",
        "GBP": "🇬🇧",
        "JPY": "🇯🇵",
        "NZD": "🇳🇿",
        "AUD": "🇦🇺",
    }

    impact = payload.get(
        "impact",
        "",
    )

    impact_icons = {
        "red": "🔴",
        "orange": "🟠",
    }

    flag = currency_flags.get(
        currency,
        "",
    )

    impact_icon = impact_icons.get(
        impact,
        "⚪",
    )

    event_name = payload.get(
        "event",
        "",
    )

    release_time = payload.get(
        "time",
        "",
    )

    actual = str(
        payload.get(
            "actual",
            "",
        )
        or ""
    ).strip()

    forecast = str(
        payload.get(
            "forecast",
            "",
        )
        or ""
    ).strip()

    previous = str(
        payload.get(
            "previous",
            "",
        )
        or ""
    ).strip()

    lines = [
        "📊 ACTUAL ECONOMIC DATA",
        "",
        f"{flag} {currency}",
        f"{impact_icon} {event_name}",
        "",
        f"⏰ Release: {release_time} MYT",
        "",
    ]

    if actual:
        lines.append(
            f"Actual: {actual}"
        )

    if forecast:
        lines.append(
            f"Forecast: {forecast}"
        )

    if previous:
        lines.append(
            f"Previous: {previous}"
        )

    return "\n".join(lines)


def _required_env(env_name) -> str:

    if not env_name:
        raise NotificationError(
            "Connector is missing required "
            "environment-variable mapping"
        )

    value = os.getenv(
        str(env_name)
    )

    if not value:
        raise NotificationError(
            "Missing required secret environment "
            f"variable '{env_name}'"
        )

    return value


def _post_json(
    url: str,
    payload: dict,
    headers: dict | None = None,
) -> str:

    body = json.dumps(
        payload
    ).encode("utf-8")

    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "DiscordBot "
            "(https://github.com/fizahkhalid/"
            "forex_factory_calendar_news_scraper, 1.0)"
        ),
    }

    if headers:
        request_headers.update(
            headers
        )

    return _perform_request(
        url,
        body,
        request_headers,
    )


def _patch_json(
    url: str,
    payload: dict,
    headers: dict | None = None,
) -> str:

    body = json.dumps(
        payload
    ).encode("utf-8")

    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "DiscordBot "
            "(https://github.com/fizahkhalid/"
            "forex_factory_calendar_news_scraper, 1.0)"
        ),
    }

    if headers:
        request_headers.update(
            headers
        )

    return _perform_patch_request(
        url,
        body,
        request_headers,
    )


def _post_form(
    url: str,
    payload: bytes,
) -> None:

    _perform_request(
        url,
        payload,
        {
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
    )


def _safe_url(
    url: str,
) -> str:

    parsed = parse.urlparse(
        url
    )

    if len(parsed.path) > 30:
        path = (
            parsed.path[:30]
            + "…"
        )
    else:
        path = parsed.path

    return (
        f"{parsed.scheme}://"
        f"{parsed.netloc}"
        f"{path}"
    )


def _perform_request(
    url: str,
    payload: bytes,
    headers: dict,
) -> str:

    safe_url = _safe_url(
        url
    )

    req = request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(
            req,
            timeout=10,
        ) as response:

            status = getattr(
                response,
                "status",
                200,
            )

            response_body = (
                response.read().decode(
                    "utf-8",
                    errors="replace",
                )
            )

            if status >= 400:
                raise NotificationError(
                    f"HTTP {status} "
                    f"from {safe_url}"
                )

            return response_body

    except HTTPError as exc:
        try:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

        except Exception:
            body = "<unreadable>"

        raise NotificationError(
            f"HTTP {exc.code} {exc.reason} "
            f"from {safe_url} — "
            f"response body: {body}"
        ) from exc

    except URLError as exc:
        raise NotificationError(
            f"Connection error to "
            f"{safe_url} — {exc.reason}"
        ) from exc


def _perform_patch_request(
    url: str,
    payload: bytes,
    headers: dict,
) -> str:

    safe_url = _safe_url(
        url
    )

    req = request.Request(
        url,
        data=payload,
        headers=headers,
        method="PATCH",
    )

    try:
        with request.urlopen(
            req,
            timeout=10,
        ) as response:

            status = getattr(
                response,
                "status",
                200,
            )

            response_body = (
                response.read().decode(
                    "utf-8",
                    errors="replace",
                )
            )

            if status >= 400:
                raise NotificationError(
                    f"HTTP {status} "
                    f"from {safe_url}"
                )

            return response_body

    except HTTPError as exc:
        try:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

        except Exception:
            body = "<unreadable>"

        raise NotificationError(
            f"HTTP {exc.code} {exc.reason} "
            f"from {safe_url} — "
            f"response body: {body}"
        ) from exc

    except URLError as exc:
        raise NotificationError(
            f"Connection error to "
            f"{safe_url} — {exc.reason}"
        ) from exc


def _perform_delete_request(
    url: str,
    headers: dict,
) -> None:

    safe_url = _safe_url(
        url
    )

    req = request.Request(
        url,
        headers=headers,
        method="DELETE",
    )

    try:
        with request.urlopen(
            req,
            timeout=10,
        ) as response:

            status = getattr(
                response,
                "status",
                204,
            )

            if status >= 400:
                raise NotificationError(
                    f"HTTP {status} "
                    f"from {safe_url}"
                )

    except HTTPError as exc:
        # If the Discord message was already
        # deleted, there is nothing left to clean up.
        if exc.code == 404:
            return

        try:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

        except Exception:
            body = "<unreadable>"

        raise NotificationError(
            f"HTTP {exc.code} {exc.reason} "
            f"from {safe_url} — "
            f"response body: {body}"
        ) from exc

    except URLError as exc:
        raise NotificationError(
            f"Connection error to "
            f"{safe_url} — {exc.reason}"
        ) from exc