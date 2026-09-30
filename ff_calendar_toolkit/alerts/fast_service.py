import json
from datetime import datetime, timezone
from pathlib import Path

import pytz

from ff_calendar_toolkit.console import AppConsole
from ff_calendar_toolkit.models import AlertOptions

from .matcher import (
    build_dedup_key,
    event_matches_rule,
    event_should_trigger,
    preview_match,
)
from .models import AlertEvent
from .notifiers import (
    NotificationError,
    NotifierFactory,
)
from .rules import load_rules
from .state import AlertStateStore


class FastAlertService:
    def __init__(
        self,
        console: AppConsole | None = None,
    ) -> None:
        self.console = console or AppConsole()

    def _cache_path(
        self,
        options: AlertOptions,
    ) -> Path:

        # options.state_dir is normally state/alerts.
        # The shared upcoming-event cache lives at:
        # state/upcoming_events.json
        return (
            options.state_dir.parent
            / "upcoming_events.json"
        )

    def _event_identity(
        self,
        record: dict,
    ) -> str:

        parts = [
            str(record.get("date", "")),
            str(record.get("time", "")),
            str(record.get("currency", "")),
            str(record.get("event", "")),
            str(record.get("impact", "")),
        ]

        return "|".join(parts)

    def _parse_event_time(
        self,
        record: dict,
    ) -> datetime | None:

        date_value = str(
            record.get(
                "date",
                "",
            )
            or ""
        ).strip()

        time_value = str(
            record.get(
                "time",
                "",
            )
            or ""
        ).strip()

        timezone_value = str(
            record.get(
                "timezone",
                "",
            )
            or ""
        ).strip()

        if (
            not date_value
            or not time_value
            or not timezone_value
        ):
            return None

        if time_value.lower() in {
            "all day",
            "tentative",
        }:
            return None

        try:
            naive = datetime.strptime(
                f"{date_value} {time_value}",
                "%d/%m/%Y %H:%M",
            )

            event_timezone = pytz.timezone(
                timezone_value
            )

            return event_timezone.localize(
                naive
            )

        except Exception:
            return None

    def _load_cached_events(
        self,
        options: AlertOptions,
    ) -> list[AlertEvent]:

        cache_path = self._cache_path(
            options
        )

        if not cache_path.exists():
            self.console.warn(
                "Upcoming-event cache does not exist yet. "
                "Run the refresh checker first."
            )
            return []

        try:
            payload = json.loads(
                cache_path.read_text(
                    encoding="utf-8"
                )
            )

        except (
            json.JSONDecodeError,
            OSError,
        ) as exc:
            self.console.warn(
                f"Could not read upcoming-event cache: {exc}"
            )
            return []

        if isinstance(payload, dict):
            records = payload.get(
                "events",
                [],
            )
        elif isinstance(payload, list):
            records = payload
        else:
            records = []

        events: list[AlertEvent] = []

        for record in records:

            if not isinstance(
                record,
                dict,
            ):
                continue

            event_time = (
                self._parse_event_time(
                    record
                )
            )

            if event_time is None:
                continue

            events.append(
                AlertEvent(
                    event_id=(
                        self._event_identity(
                            record
                        )
                    ),
                    event_time=event_time,
                    payload=record,
                )
            )

        events.sort(
            key=lambda event: (
                event.event_time
            )
        )

        return events

    def run(
        self,
        options: AlertOptions,
    ) -> int:

        started_at = datetime.now(
            timezone.utc
        )

        self.console.step(
            "Running FAST 10-minute alert check..."
        )

        # Separate state file so the fast job
        # does not fight with Actual-alert state.
        state_store = AlertStateStore(
            options.state_dir,
            filename="fast_alert_state.json",
        )

        state = state_store.load()

        rules = load_rules(
            options.rules_dir
        )

        events = self._load_cached_events(
            options
        )

        notifier_factory = NotifierFactory(
            options
        )

        self.console.step(
            f"Loaded {len(rules)} rules, "
            f"{len(events)} cached events, "
            f"and "
            f"{len(notifier_factory.connector_ids())} "
            f"enabled connectors"
        )

        triggered = 0
        delivered = 0

        for rule in rules:

            if not rule.enabled:
                continue

            for event in events:

                if not event_matches_rule(
                    rule,
                    event,
                ):
                    continue

                if not event_should_trigger(
                    rule,
                    event,
                    started_at,
                    options.check_interval_minutes,
                ):
                    continue

                triggered += 1

                dedup_key = build_dedup_key(
                    rule,
                    event,
                )

                for connector_id in rule.deliver:

                    if state_store.is_sent(
                        state,
                        dedup_key,
                        connector_id,
                    ):
                        continue

                    preview = preview_match(
                        rule,
                        event,
                        connector_id,
                        "queued",
                    )

                    preview_payload = {
                        "rule": preview.rule_name,
                        "event": preview.event_name,
                        "currency": preview.currency,
                        "impact": preview.impact,
                        "trigger_at": (
                            preview.trigger_at.isoformat()
                        ),
                        "connector": connector_id,
                    }

                    try:
                        notifier_factory.send(
                            connector_id,
                            rule,
                            event,
                        )

                        state_store.mark_sent(
                            state,
                            dedup_key,
                            connector_id,
                            preview_payload,
                        )

                        delivered += 1

                        self.console.success(
                            f"FAST alert sent for "
                            f"'{rule.name}' via "
                            f"{connector_id}: "
                            f"{event.payload.get('event', '')}"
                        )

                    except NotificationError as exc:

                        state_store.mark_failure(
                            state,
                            dedup_key,
                            connector_id,
                            str(exc),
                        )

                        self.console.warn(
                            f"FAST alert failed for "
                            f"'{rule.name}' via "
                            f"{connector_id}: {exc}"
                        )

        state_store.save(
            state
        )

        self.console.success(
            f"Fast alert check complete: "
            f"{triggered} trigger matches evaluated, "
            f"{delivered} notifications delivered"
        )

        return 0