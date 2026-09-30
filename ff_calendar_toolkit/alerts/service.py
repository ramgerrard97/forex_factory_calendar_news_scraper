import json
from datetime import datetime, timedelta, timezone

from ff_calendar_toolkit.config import (
    DEFAULT_ALLOWED_CURRENCY_CODES,
    DEFAULT_ALLOWED_IMPACT_COLORS,
    DEFAULT_HEADLESS,
    DEFAULT_TARGET_TIMEZONE,
)
from ff_calendar_toolkit.normalize import normalize_rows
from ff_calendar_toolkit.scraper import ForexFactoryScraper
from ff_calendar_toolkit.storage import FileOutputStore
from ff_calendar_toolkit.console import AppConsole
from ff_calendar_toolkit.models import AlertOptions

from .events import load_alert_events
from .matcher import (
    build_dedup_key,
    event_matches_rule,
    event_should_trigger,
    preview_match,
)
from .notifiers import (
    NotificationError,
    NotifierFactory,
    render_actual_message,
)
from .rules import load_rules
from .state import AlertStateStore


ACTUAL_ALERT_WINDOW_MINUTES = 20


class AlertService:
    def __init__(
        self,
        console: AppConsole | None = None,
    ) -> None:
        self.console = console or AppConsole()

    def run(
        self,
        options: AlertOptions,
    ) -> int:

        state_store = AlertStateStore(
            options.state_dir
        )

        self.console.step(
            "Refreshing Forex Factory calendar data..."
        )

        scraper = ForexFactoryScraper(
            self.console,
            headless=DEFAULT_HEADLESS,
        )

        raw_rows, context = scraper.scrape_month(
            "this",
            DEFAULT_TARGET_TIMEZONE,
        )

        records = normalize_rows(
            raw_rows,
            context.year,
            context.source_timezone,
            DEFAULT_TARGET_TIMEZONE,
            DEFAULT_ALLOWED_CURRENCY_CODES,
            DEFAULT_ALLOWED_IMPACT_COLORS,
            context.scraped_at,
        )

        store = FileOutputStore(
            options.output_dir
        )

        store.begin_run(
            "both"
        )

        store.write(
            records,
            context,
            "both",
        )

        self.console.success(
            f"Refreshed {len(records)} "
            f"Forex Factory events"
        )

        state = state_store.load()

        rules = load_rules(
            options.rules_dir
        )

        events = load_alert_events(
            options.output_dir
        )

        notifier_factory = NotifierFactory(
            options
        )

        now = datetime.now(
            timezone.utc
        )

        self.console.step(
            f"Loaded {len(rules)} rules, "
            f"{len(events)} events, and "
            f"{len(notifier_factory.connector_ids())} "
            f"enabled connectors"
        )

        actual_alerted = 0

        # -------------------------------------------------
        # ACTUAL DATA ALERTS ONLY
        #
        # 10-minute pre-news alerts are intentionally NOT
        # handled here anymore.
        #
        # They are handled by alerts-fast so they do not
        # have to wait for this slow Forex Factory scrape.
        # -------------------------------------------------

        for rule in rules:

            if not rule.enabled:
                self.console.warn(
                    f"Skipping disabled rule "
                    f"'{rule.name}'"
                )
                continue

            for event in events:

                if not event_matches_rule(
                    rule,
                    event,
                ):
                    continue

                actual = str(
                    event.payload.get(
                        "actual",
                        "",
                    )
                    or ""
                ).strip()

                actual_is_valid = (
                    actual
                    and actual.lower()
                    not in {
                        "empty",
                        "none",
                        "null",
                        "nan",
                    }
                )

                event_time_utc = (
                    event.event_time.astimezone(
                        timezone.utc
                    )
                )

                event_age = (
                    now
                    - event_time_utc
                )

                actual_is_recent = (
                    timedelta(0)
                    <= event_age
                    <= timedelta(
                        minutes=(
                            ACTUAL_ALERT_WINDOW_MINUTES
                        )
                    )
                )

                if not (
                    actual_is_valid
                    and actual_is_recent
                ):
                    continue

                actual_dedup_key = (
                    f"actual|"
                    f"{event.payload.get('date', '')}|"
                    f"{event.payload.get('time', '')}|"
                    f"{event.payload.get('currency', '')}|"
                    f"{event.payload.get('event', '')}|"
                    f"{event.payload.get('impact', '')}"
                )

                for connector_id in rule.deliver:

                    if state_store.is_sent(
                        state,
                        actual_dedup_key,
                        connector_id,
                    ):
                        continue

                    try:
                        actual_message = (
                            render_actual_message(
                                event
                            )
                        )

                        notifier_factory.send_raw(
                            connector_id,
                            actual_message,
                        )

                        state_store.mark_sent(
                            state,
                            actual_dedup_key,
                            connector_id,
                            {
                                "rule": (
                                    rule.name
                                ),
                                "event": (
                                    event.payload.get(
                                        "event",
                                        "",
                                    )
                                ),
                                "currency": (
                                    event.payload.get(
                                        "currency",
                                        "",
                                    )
                                ),
                                "impact": (
                                    event.payload.get(
                                        "impact",
                                        "",
                                    )
                                ),
                                "actual": actual,
                                "connector": (
                                    connector_id
                                ),
                            },
                        )

                        actual_alerted += 1

                        self.console.success(
                            f"Sent actual alert "
                            f"for '{rule.name}' "
                            f"via {connector_id}: "
                            f"{event.payload.get('event', '')}"
                        )

                    except NotificationError as exc:

                        state_store.mark_failure(
                            state,
                            actual_dedup_key,
                            connector_id,
                            str(exc),
                        )

                        self.console.warn(
                            f"Failed to send "
                            f"actual alert for "
                            f"'{rule.name}' via "
                            f"{connector_id}: "
                            f"{exc}"
                        )

        # -------------------------------------------------
        # REFRESH UPCOMING-EVENT CACHE
        # AND WEEKLY DISCORD CALENDAR
        # -------------------------------------------------

        try:
            from ff_calendar_toolkit.weekly import (
                WeeklyCalendarService,
            )

            weekly_service = (
                WeeklyCalendarService(
                    self.console
                )
            )

            monday, friday = (
                weekly_service.get_week_dates()
            )

            month_selectors = (
                weekly_service.get_month_selectors(
                    monday,
                    friday,
                )
            )

            weekly_records = (
                weekly_service.scrape_week(
                    monday,
                    friday,
                    month_selectors,
                    DEFAULT_TARGET_TIMEZONE,
                    DEFAULT_ALLOWED_CURRENCY_CODES,
                    DEFAULT_ALLOWED_IMPACT_COLORS,
                )
            )

            # ---------------------------------------------
            # Save upcoming-event cache for alerts-fast.
            # ---------------------------------------------

            cache_path = (
                options.state_dir.parent
                / "upcoming_events.json"
            )

            cache_payload = {
                "generated_at": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
                "week_start": (
                    monday.isoformat()
                ),
                "week_end": (
                    friday.isoformat()
                ),
                "events": weekly_records,
            }

            cache_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            cache_path.write_text(
                json.dumps(
                    cache_payload,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.console.success(
                f"Updated fast-alert cache "
                f"with "
                f"{len(weekly_records)} "
                f"upcoming events"
            )

            # ---------------------------------------------
            # Update weekly Discord calendar.
            #
            # format_calendar() may return one message or
            # multiple Part 1/2, Part 2/2 messages.
            # ---------------------------------------------

            weekly_messages = (
                weekly_service.format_calendar(
                    weekly_records,
                    monday,
                    friday,
                )
            )

            weekly_service.update_discord_message(
                weekly_messages,
                options,
            )

        except Exception as exc:

            self.console.warn(
                f"Weekly Discord calendar "
                f"update failed: {exc}"
            )

        # Save Actual-alert dedup state.
        state_store.save(
            state
        )

        self.console.success(
            f"Refresh check complete: "
            f"{actual_alerted} actual "
            f"notifications delivered. "
            f"10-minute alerts are handled "
            f"separately by alerts-fast."
        )

        return 0


def preview_alerts(
    options: AlertOptions,
) -> list[dict]:

    rules = load_rules(
        options.rules_dir
    )

    events = load_alert_events(
        options.output_dir
    )

    state_store = AlertStateStore(
        options.state_dir
    )

    state = state_store.load()

    now = datetime.now(
        timezone.utc
    )

    previews = []

    for rule in rules:

        if not rule.enabled:
            continue

        for event in events:

            if not event_matches_rule(
                rule,
                event,
            ):
                continue

            dedup_key = build_dedup_key(
                rule,
                event,
            )

            triggered = event_should_trigger(
                rule,
                event,
                now,
                options.check_interval_minutes,
            )

            if (
                event.event_time
                < now.astimezone(
                    event.event_time.tzinfo
                )
            ):
                status = "past"

            elif triggered:
                status = "due"

            else:
                status = "upcoming"

            for connector_id in rule.deliver:

                if state_store.is_sent(
                    state,
                    dedup_key,
                    connector_id,
                ):
                    connector_status = (
                        "sent"
                    )

                else:
                    connector_status = (
                        status
                    )

                preview = preview_match(
                    rule,
                    event,
                    connector_id,
                    connector_status,
                )

                previews.append(
                    {
                        "rule": (
                            preview.rule_name
                        ),
                        "connector": (
                            connector_id
                        ),
                        "event": (
                            preview.event_name
                        ),
                        "currency": (
                            preview.currency
                        ),
                        "impact": (
                            preview.impact
                        ),
                        "trigger_at": (
                            preview.trigger_at.isoformat()
                        ),
                        "event_time": (
                            preview.event_time.isoformat()
                        ),
                        "timezone": (
                            preview.timezone
                        ),
                        "status": (
                            connector_status
                        ),
                    }
                )

    return sorted(
        previews,
        key=lambda item: (
            item["trigger_at"],
            item["rule"],
            item["connector"],
        ),
    )