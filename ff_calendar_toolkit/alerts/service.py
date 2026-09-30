import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

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

FOREX_FACTORY_FEED_URL = (
    "https://nfs.faireconomy.media/"
    "ff_calendar_thisweek.json"
)

FOREX_FACTORY_FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.forexfactory.com/",
}

FEED_IMPACT_MAP = {
    "high": "red",
    "medium": "orange",
    "low": "yellow",
    "holiday": "gray",
}


def _clean_text(value) -> str:
    return " ".join(
        str(value or "").split()
    )


def _event_key(
    record: dict,
) -> tuple[str, str]:

    currency = _clean_text(
        record.get("currency")
        or record.get("country")
    ).upper()

    event_name = _clean_text(
        record.get("event")
        or record.get("title")
    ).casefold()

    return (
        currency,
        event_name,
    )


def _is_real_value(
    value,
) -> bool:

    text = _clean_text(
        value
    )

    return bool(
        text
        and text.casefold()
        not in {
            "empty",
            "none",
            "null",
            "nan",
        }
    )


def _record_local_datetime(
    record: dict,
) -> datetime | None:

    date_text = _clean_text(
        record.get("date")
    )

    time_text = _clean_text(
        record.get("time")
    )

    if (
        not date_text
        or not time_text
    ):
        return None

    try:
        parsed = datetime.strptime(
            f"{date_text} {time_text}",
            "%d/%m/%Y %H:%M",
        )

    except ValueError:
        return None

    return parsed.replace(
        tzinfo=ZoneInfo(
            DEFAULT_TARGET_TIMEZONE
        )
    )


def _fetch_week_feed(
    console: AppConsole,
) -> list[dict]:

    console.step(
        "Downloading authoritative "
        "Forex Factory weekly JSON schedule"
    )

    request = Request(
        FOREX_FACTORY_FEED_URL,
        headers=(
            FOREX_FACTORY_FEED_HEADERS
        ),
    )

    try:
        with urlopen(
            request,
            timeout=20,
        ) as response:

            payload = json.load(
                response
            )

    except HTTPError as exc:

        raise RuntimeError(
            "Forex Factory schedule feed "
            f"returned HTTP {exc.code}. "
            "Existing fast-alert cache "
            "will NOT be overwritten."
        ) from exc

    except URLError as exc:

        raise RuntimeError(
            "Could not reach Forex Factory "
            "schedule feed. Existing "
            "fast-alert cache will NOT "
            "be overwritten."
        ) from exc

    except Exception as exc:

        raise RuntimeError(
            "Could not read Forex Factory "
            "schedule feed. Existing "
            "fast-alert cache will NOT "
            "be overwritten."
        ) from exc

    if not isinstance(
        payload,
        list,
    ):
        raise RuntimeError(
            "Forex Factory schedule feed "
            "returned an unexpected format."
        )

    console.success(
        f"Downloaded {len(payload)} "
        "raw events from the weekly feed"
    )

    return payload


def _build_schedule_records(
    raw_feed: list[dict],
    monday,
    friday,
) -> list[dict]:

    target_zone = ZoneInfo(
        DEFAULT_TARGET_TIMEZONE
    )

    allowed_currencies = {
        str(code).upper()
        for code
        in DEFAULT_ALLOWED_CURRENCY_CODES
    }

    allowed_impacts = {
        str(color).lower()
        for color
        in DEFAULT_ALLOWED_IMPACT_COLORS
    }

    scraped_at = datetime.now(
        timezone.utc
    ).isoformat()

    records = []

    for item in raw_feed:

        title = _clean_text(
            item.get("title")
        )

        currency = _clean_text(
            item.get("country")
        ).upper()

        raw_impact = _clean_text(
            item.get("impact")
        ).casefold()

        impact = FEED_IMPACT_MAP.get(
            raw_impact
        )

        raw_date = _clean_text(
            item.get("date")
        )

        if (
            not title
            or not currency
            or not impact
            or not raw_date
        ):
            continue

        if (
            currency
            not in allowed_currencies
        ):
            continue

        if (
            impact
            not in allowed_impacts
        ):
            continue

        try:
            source_time = (
                datetime.fromisoformat(
                    raw_date
                )
            )

        except ValueError:
            continue

        if source_time.tzinfo is None:
            continue

        malaysia_time = (
            source_time.astimezone(
                target_zone
            )
        )

        local_date = (
            malaysia_time.date()
        )

        if (
            local_date < monday
            or local_date > friday
        ):
            continue

        records.append(
            {
                "time": (
                    malaysia_time.strftime(
                        "%H:%M"
                    )
                ),
                "timezone": (
                    DEFAULT_TARGET_TIMEZONE
                ),
                "currency": currency,
                "impact": impact,
                "event": title,
                "detail": "",
                "actual": "",
                "forecast": _clean_text(
                    item.get("forecast")
                ),
                "previous": _clean_text(
                    item.get("previous")
                ),
                "day": (
                    malaysia_time.strftime(
                        "%a"
                    )
                ),
                "date": (
                    malaysia_time.strftime(
                        "%d/%m/%Y"
                    )
                ),
                "scraped_at": (
                    scraped_at
                ),
            }
        )

    records.sort(
        key=lambda record: (
            _record_local_datetime(
                record
            )
            or datetime.max.replace(
                tzinfo=target_zone
            ),
            record.get(
                "currency",
                "",
            ),
            record.get(
                "event",
                "",
            ),
        )
    )

    return records


def _apply_authoritative_times(
    records: list[dict],
    schedule_records: list[dict],
) -> int:

    schedule_by_key: dict[
        tuple[str, str],
        list[dict],
    ] = {}

    for schedule in schedule_records:

        schedule_by_key.setdefault(
            _event_key(
                schedule
            ),
            [],
        ).append(
            schedule
        )

    patched = 0

    for record in records:

        candidates = (
            schedule_by_key.get(
                _event_key(
                    record
                ),
                [],
            )
        )

        if not candidates:
            continue

        record_dt = (
            _record_local_datetime(
                record
            )
        )

        usable_candidates = (
            candidates
        )

        if record_dt is not None:

            near_candidates = []

            for candidate in candidates:

                candidate_dt = (
                    _record_local_datetime(
                        candidate
                    )
                )

                if candidate_dt is None:
                    continue

                day_difference = abs(
                    (
                        candidate_dt.date()
                        - record_dt.date()
                    ).days
                )

                if day_difference <= 1:
                    near_candidates.append(
                        candidate
                    )

            if near_candidates:

                usable_candidates = (
                    near_candidates
                )

            elif len(candidates) > 1:

                continue

            elif len(candidates) == 1:

                candidate_dt = (
                    _record_local_datetime(
                        candidates[0]
                    )
                )

                if candidate_dt is not None:

                    day_difference = abs(
                        (
                            candidate_dt.date()
                            - record_dt.date()
                        ).days
                    )

                    if day_difference > 1:
                        continue

        if (
            len(
                usable_candidates
            )
            == 1
        ):

            chosen = (
                usable_candidates[0]
            )

        elif record_dt is not None:

            def distance(
                candidate,
            ):

                candidate_dt = (
                    _record_local_datetime(
                        candidate
                    )
                )

                if candidate_dt is None:
                    return float(
                        "inf"
                    )

                return abs(
                    (
                        candidate_dt
                        - record_dt
                    ).total_seconds()
                )

            chosen = min(
                usable_candidates,
                key=distance,
            )

        else:

            chosen = (
                usable_candidates[0]
            )

        record["time"] = (
            chosen["time"]
        )

        record["timezone"] = (
            chosen["timezone"]
        )

        record["date"] = (
            chosen["date"]
        )

        record["day"] = (
            chosen["day"]
        )

        if not _is_real_value(
            record.get(
                "forecast"
            )
        ):
            record["forecast"] = (
                chosen.get(
                    "forecast",
                    "",
                )
            )

        if not _is_real_value(
            record.get(
                "previous"
            )
        ):
            record["previous"] = (
                chosen.get(
                    "previous",
                    "",
                )
            )

        patched += 1

    return patched


def _merge_schedule_with_scraped(
    schedule_records: list[dict],
    scraped_records: list[dict],
) -> list[dict]:

    scraped_by_key: dict[
        tuple[str, str],
        list[dict],
    ] = {}

    for record in scraped_records:

        scraped_by_key.setdefault(
            _event_key(
                record
            ),
            [],
        ).append(
            record
        )

    merged = []

    for schedule in schedule_records:

        output = dict(
            schedule
        )

        candidates = (
            scraped_by_key.get(
                _event_key(
                    schedule
                ),
                [],
            )
        )

        schedule_dt = (
            _record_local_datetime(
                schedule
            )
        )

        near_candidates = []

        for candidate in candidates:

            candidate_dt = (
                _record_local_datetime(
                    candidate
                )
            )

            if (
                schedule_dt is None
                or candidate_dt is None
            ):
                near_candidates.append(
                    candidate
                )
                continue

            day_difference = abs(
                (
                    candidate_dt.date()
                    - schedule_dt.date()
                ).days
            )

            if day_difference <= 1:

                near_candidates.append(
                    candidate
                )

        candidates = (
            near_candidates
        )

        if candidates:

            if schedule_dt is not None:

                def distance(
                    candidate,
                ):

                    candidate_dt = (
                        _record_local_datetime(
                            candidate
                        )
                    )

                    if candidate_dt is None:
                        return float(
                            "inf"
                        )

                    return abs(
                        (
                            candidate_dt
                            - schedule_dt
                        ).total_seconds()
                    )

                source = min(
                    candidates,
                    key=distance,
                )

            else:

                source = (
                    candidates[0]
                )

            if _is_real_value(
                source.get(
                    "actual"
                )
            ):

                output["actual"] = (
                    source.get(
                        "actual",
                        "",
                    )
                )

            if _is_real_value(
                source.get(
                    "detail"
                )
            ):

                output["detail"] = (
                    source.get(
                        "detail",
                        "",
                    )
                )

            if (
                not _is_real_value(
                    output.get(
                        "forecast"
                    )
                )
                and _is_real_value(
                    source.get(
                        "forecast"
                    )
                )
            ):

                output["forecast"] = (
                    source.get(
                        "forecast",
                        "",
                    )
                )

            if (
                not _is_real_value(
                    output.get(
                        "previous"
                    )
                )
                and _is_real_value(
                    source.get(
                        "previous"
                    )
                )
            ):

                output["previous"] = (
                    source.get(
                        "previous",
                        "",
                    )
                )

        merged.append(
            output
        )

    return merged


def _write_fast_cache(
    options: AlertOptions,
    monday,
    friday,
    schedule_records: list[dict],
) -> None:

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
        "events": (
            schedule_records
        ),
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


class AlertService:
    def __init__(
        self,
        console: AppConsole | None = None,
    ) -> None:

        self.console = (
            console
            or AppConsole()
        )

    def run(
        self,
        options: AlertOptions,
    ) -> int:

        state_store = (
            AlertStateStore(
                options.state_dir
            )
        )

        # -------------------------------------------------
        # GET WEEK RANGE
        # -------------------------------------------------

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

        malaysia_now = (
            datetime.now(
                ZoneInfo(
                    DEFAULT_TARGET_TIMEZONE
                )
            )
        )

        # Saturday has no future Monday-Friday events
        # inside Forex Factory's current weekly export.
        # Keep the existing cache until Sunday starts
        # the new Forex Factory week.
        if malaysia_now.weekday() == 5:

            self.console.step(
                "Saturday Malaysia time: "
                "keeping the existing "
                "Monday-Friday alert cache."
            )

            return 0

        # -------------------------------------------------
        # AUTHORITATIVE WEEKLY SCHEDULE
        #
        # The JSON feed timestamp includes a UTC offset.
        # We convert that directly to Malaysia time.
        #
        # This is now the source of truth for:
        #
        # - event date
        # - event time
        # - fast-alert cache
        # - weekly Discord schedule
        #
        # Selenium is NOT trusted for event time anymore.
        # -------------------------------------------------

        try:

            raw_feed = (
                _fetch_week_feed(
                    self.console
                )
            )

            schedule_records = (
                _build_schedule_records(
                    raw_feed,
                    monday,
                    friday,
                )
            )

            if not schedule_records:

                raise RuntimeError(
                    "Forex Factory weekly feed "
                    "contained no matching "
                    "Monday-Friday red/orange "
                    "events for the configured "
                    "currencies."
                )

            _write_fast_cache(
                options,
                monday,
                friday,
                schedule_records,
            )

            self.console.success(
                "Updated fast-alert cache "
                f"with "
                f"{len(schedule_records)} "
                "authoritative "
                "Malaysia-time events"
            )

        except Exception as exc:

            self.console.warn(
                "Authoritative schedule "
                f"refresh failed: {exc}"
            )

            self.console.warn(
                "Existing upcoming-events "
                "cache was left unchanged."
            )

            return 1

        # -------------------------------------------------
        # SELENIUM SCRAPE
        #
        # We still scrape Forex Factory because we want
        # live Actual values and detail information.
        #
        # But the Selenium-rendered clock is no longer
        # trusted.
        # -------------------------------------------------

        self.console.step(
            "Refreshing Forex Factory "
            "calendar data for Actual values..."
        )

        scraper = (
            ForexFactoryScraper(
                self.console,
                headless=DEFAULT_HEADLESS,
            )
        )

        try:

            raw_rows, context = (
                scraper.scrape_month(
                    "this",
                    DEFAULT_TARGET_TIMEZONE,
                )
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

        except Exception as exc:

            # The authoritative fast-alert cache is already
            # correct. Do not destroy that just because
            # Selenium failed.
            self.console.warn(
                "Selenium calendar refresh "
                f"failed: {exc}"
            )

            self.console.warn(
                "Fast-alert schedule was "
                "successfully refreshed, "
                "but Actual/calendar update "
                "was skipped."
            )

            return 0

        # Replace Selenium event times with the
        # authoritative feed times before writing
        # the normalized calendar files.
        patched_records = (
            _apply_authoritative_times(
                records,
                schedule_records,
            )
        )

        self.console.success(
            "Corrected "
            f"{patched_records} "
            "scraped event times using "
            "the authoritative weekly feed"
        )

        store = (
            FileOutputStore(
                options.output_dir
            )
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
            f"Refreshed "
            f"{len(records)} "
            "Forex Factory events"
        )

        # -------------------------------------------------
        # LOAD ACTUAL ALERT SYSTEM
        # -------------------------------------------------

        state = (
            state_store.load()
        )

        rules = load_rules(
            options.rules_dir
        )

        events = load_alert_events(
            options.output_dir
        )

        notifier_factory = (
            NotifierFactory(
                options
            )
        )

        now = datetime.now(
            timezone.utc
        )

        self.console.step(
            f"Loaded {len(rules)} rules, "
            f"{len(events)} events, and "
            f"{len(notifier_factory.connector_ids())} "
            "enabled connectors"
        )

        actual_alerted = 0

        # -------------------------------------------------
        # ACTUAL DATA ALERTS ONLY
        #
        # 10-minute reminders are handled by alerts-fast.
        # -------------------------------------------------

        for rule in rules:

            if not rule.enabled:

                self.console.warn(
                    "Skipping disabled rule "
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

                for connector_id in (
                    rule.deliver
                ):

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
                                "actual": (
                                    actual
                                ),
                                "connector": (
                                    connector_id
                                ),
                            },
                        )

                        actual_alerted += 1

                        self.console.success(
                            "Sent actual alert "
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
                            "Failed to send "
                            "actual alert for "
                            f"'{rule.name}' via "
                            f"{connector_id}: "
                            f"{exc}"
                        )

        # -------------------------------------------------
        # WEEKLY DISCORD CALENDAR
        #
        # Schedule/date/time comes from JSON feed.
        # Actual/detail is merged in from Selenium.
        # -------------------------------------------------

        try:

            month_selectors = (
                weekly_service.get_month_selectors(
                    monday,
                    friday,
                )
            )

            scraped_weekly_records = (
                weekly_service.scrape_week(
                    monday,
                    friday,
                    month_selectors,
                    DEFAULT_TARGET_TIMEZONE,
                    DEFAULT_ALLOWED_CURRENCY_CODES,
                    DEFAULT_ALLOWED_IMPACT_COLORS,
                )
            )

            weekly_records = (
                _merge_schedule_with_scraped(
                    schedule_records,
                    scraped_weekly_records,
                )
            )

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

            self.console.success(
                "Weekly Discord calendar "
                "updated using authoritative "
                "Malaysia event times"
            )

        except Exception as exc:

            self.console.warn(
                "Weekly Discord calendar "
                f"update failed: {exc}"
            )

        # -------------------------------------------------
        # SAVE ACTUAL ALERT DEDUP STATE
        # -------------------------------------------------

        state_store.save(
            state
        )

        self.console.success(
            "Refresh check complete: "
            f"{actual_alerted} actual "
            "notifications delivered. "
            "10-minute alerts are handled "
            "separately by alerts-fast."
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

    state_store = (
        AlertStateStore(
            options.state_dir
        )
    )

    state = (
        state_store.load()
    )

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

            dedup_key = (
                build_dedup_key(
                    rule,
                    event,
                )
            )

            triggered = (
                event_should_trigger(
                    rule,
                    event,
                    now,
                    options.check_interval_minutes,
                )
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

            for connector_id in (
                rule.deliver
            ):

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

                preview = (
                    preview_match(
                        rule,
                        event,
                        connector_id,
                        connector_status,
                    )
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