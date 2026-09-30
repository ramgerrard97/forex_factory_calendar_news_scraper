from datetime import date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .console import AppConsole
from .normalize import normalize_rows
from .scraper import ForexFactoryScraper


MALAYSIA_TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")

# Discord hard limit is 2000 characters.
# We stay below it for safety.
DISCORD_SAFE_LIMIT = 1900
DISCORD_CONTENT_LIMIT = 1700


class WeeklyCalendarService:
    def __init__(
        self,
        console: AppConsole | None = None,
    ) -> None:
        self.console = console or AppConsole()

    def malaysia_now(self) -> datetime:
        return datetime.now(
            MALAYSIA_TIMEZONE
        )

    def get_week_dates(
        self,
    ) -> tuple[date, date]:

        today = (
            self.malaysia_now()
            .date()
        )

        weekday = today.weekday()

        if weekday == 6:
            # Sunday -> upcoming Monday
            monday = (
                today
                + timedelta(days=1)
            )

        elif weekday == 5:
            # Saturday -> upcoming Monday
            monday = (
                today
                + timedelta(days=2)
            )

        else:
            # Monday-Friday ->
            # current week's Monday
            monday = (
                today
                - timedelta(
                    days=weekday
                )
            )

        friday = (
            monday
            + timedelta(days=4)
        )

        return monday, friday

    def get_month_selectors(
        self,
        monday: date,
        friday: date,
    ) -> list[str]:

        today = (
            self.malaysia_now()
            .date()
        )

        # Week completely inside one month.
        if (
            monday.month
            == friday.month
            and monday.year
            == friday.year
        ):
            if (
                monday.month
                == today.month
                and monday.year
                == today.year
            ):
                return ["this"]

            return ["next"]

        # Week crosses into next month.
        return ["this", "next"]

    def scrape_week(
        self,
        monday: date,
        friday: date,
        month_selectors: list[str],
        target_timezone: str,
        allowed_currencies: list[str],
        allowed_impacts: list[str],
    ) -> list[dict]:

        scraper = ForexFactoryScraper(
            self.console,
            headless=True,
        )

        weekly_records: list[dict] = []

        for month in month_selectors:

            self.console.step(
                f"Scraping month selector "
                f"'{month}'"
            )

            raw_rows, context = (
                scraper.scrape_month(
                    month,
                    target_timezone,
                )
            )

            self.console.step(
                f"Normalizing "
                f"{len(raw_rows)} raw rows "
                f"for {context.month_name} "
                f"{context.year}"
            )

            records = normalize_rows(
                raw_rows,
                context.year,
                context.source_timezone,
                target_timezone,
                allowed_currencies,
                allowed_impacts,
                context.scraped_at,
            )

            for record in records:

                try:
                    event_date = (
                        datetime.strptime(
                            record["date"],
                            "%d/%m/%Y",
                        )
                        .date()
                    )

                except (
                    ValueError,
                    TypeError,
                    KeyError,
                ):
                    continue

                if (
                    monday
                    <= event_date
                    <= friday
                ):
                    weekly_records.append(
                        record
                    )

        weekly_records.sort(
            key=lambda record: (
                datetime.strptime(
                    record["date"],
                    "%d/%m/%Y",
                ),
                record.get(
                    "time",
                    "",
                ),
            )
        )

        return weekly_records

    def _format_event_line(
        self,
        record: dict,
    ) -> str:

        currency_flags = {
            "USD": "🇺🇸",
            "EUR": "🇪🇺",
            "GBP": "🇬🇧",
            "JPY": "🇯🇵",
            "NZD": "🇳🇿",
            "AUD": "🇦🇺",
        }

        impact_icons = {
            "red": "🔴",
            "orange": "🟠",
        }

        currency = str(
            record.get(
                "currency",
                "",
            )
            or ""
        ).strip()

        flag = currency_flags.get(
            currency,
            "",
        )

        impact = impact_icons.get(
            str(
                record.get(
                    "impact",
                    "",
                )
                or ""
            ).strip(),
            "⚪",
        )

        event_name = str(
            record.get(
                "event",
                "",
            )
            or ""
        ).strip()

        event_time = str(
            record.get(
                "time",
                "",
            )
            or ""
        ).strip()

        actual = str(
            record.get(
                "actual",
                "",
            )
            or ""
        ).strip()

        forecast = str(
            record.get(
                "forecast",
                "",
            )
            or ""
        ).strip()

        previous = str(
            record.get(
                "previous",
                "",
            )
            or ""
        ).strip()

        values: list[str] = []

        if actual:
            values.append(
                f"A:{actual}"
            )

        if forecast:
            values.append(
                f"F:{forecast}"
            )

        if previous:
            values.append(
                f"P:{previous}"
            )

        event_line = (
            f"{event_time} MYT — "
            f"{flag} {currency} "
            f"{impact} "
            f"{event_name}"
        )

        if values:
            event_line += (
                " | "
                + " · ".join(values)
            )

        return event_line

    def format_calendar(
        self,
        records: list[dict],
        monday: date,
        friday: date,
    ) -> list[str]:

        date_range = (
            f"{monday.strftime('%d %b')} – "
            f"{friday.strftime('%d %b %Y')}"
        )

        if not records:
            return [
                (
                    "📅 **WEEKLY ECONOMIC "
                    "CALENDAR**\n"
                    f"{date_range}\n\n"
                    "No high or medium impact "
                    "events found."
                )
            ]

        day_groups: list[
            tuple[date, list[str]]
        ] = []

        current_date: date | None = None
        current_events: list[str] = []

        for record in records:

            try:
                event_date = (
                    datetime.strptime(
                        record["date"],
                        "%d/%m/%Y",
                    )
                    .date()
                )

            except (
                ValueError,
                TypeError,
                KeyError,
            ):
                continue

            event_line = (
                self._format_event_line(
                    record
                )
            )

            if (
                current_date
                is None
            ):
                current_date = event_date

            if (
                event_date
                != current_date
            ):
                day_groups.append(
                    (
                        current_date,
                        current_events,
                    )
                )

                current_date = event_date
                current_events = []

            current_events.append(
                event_line
            )

        if (
            current_date
            is not None
        ):
            day_groups.append(
                (
                    current_date,
                    current_events,
                )
            )

        content_parts: list[list[str]] = []
        current_lines: list[str] = []

        def current_length(
            lines: list[str],
        ) -> int:
            return len(
                "\n".join(lines)
            )

        for (
            event_date,
            event_lines,
        ) in day_groups:

            day_heading = (
                f"**"
                f"{event_date.strftime('%A').upper()}"
                f"**"
            )

            first_event_of_day = True

            for event_line in event_lines:

                if first_event_of_day:
                    addition = [
                        "",
                        day_heading,
                        event_line,
                    ]

                else:
                    addition = [
                        event_line,
                    ]

                candidate = (
                    current_lines
                    + addition
                )

                if (
                    current_lines
                    and current_length(
                        candidate
                    )
                    > DISCORD_CONTENT_LIMIT
                ):
                    content_parts.append(
                        current_lines
                    )

                    current_lines = [
                        (
                            f"**"
                            f"{event_date.strftime('%A').upper()}"
                            f" — CONT.**"
                        ),
                        event_line,
                    ]

                else:
                    current_lines = candidate

                first_event_of_day = False

        if current_lines:
            content_parts.append(
                current_lines
            )

        if not content_parts:
            content_parts = [
                [
                    (
                        "No high or medium "
                        "impact events found."
                    )
                ]
            ]

        total_parts = len(
            content_parts
        )

        messages: list[str] = []

        for index, lines in enumerate(
            content_parts,
            start=1,
        ):

            if total_parts == 1:
                title = (
                    "📅 **WEEKLY ECONOMIC "
                    "CALENDAR**"
                )

            else:
                title = (
                    "📅 **WEEKLY ECONOMIC "
                    f"CALENDAR — PART "
                    f"{index}/{total_parts}**"
                )

            body = (
                "\n".join(lines)
                .strip()
            )

            message = (
                f"{title}\n"
                f"{date_range}\n\n"
                f"{body}"
            )

            # Additional emergency protection.
            # Normally this should never happen.
            if (
                len(message)
                > DISCORD_SAFE_LIMIT
            ):
                raise ValueError(
                    "Generated weekly calendar "
                    "part exceeded Discord's "
                    "safe message limit."
                )

            messages.append(
                message
            )

        return messages

    def send_to_discord(
        self,
        messages: list[str] | str,
        alert_options,
    ) -> None:

        from .alerts.notifiers import (
            NotifierFactory,
        )

        if isinstance(
            messages,
            str,
        ):
            messages = [messages]

        notifier = NotifierFactory(
            alert_options
        )

        message_ids: list[str] = []

        try:
            for message in messages:

                message_id = (
                    notifier.send_raw(
                        "discord_main",
                        message,
                    )
                )

                if not message_id:
                    raise RuntimeError(
                        "Discord did not "
                        "return a message ID."
                    )

                message_ids.append(
                    str(message_id)
                )

        except Exception:

            # Clean up any new messages
            # already created if sending
            # another part fails.
            for message_id in message_ids:
                try:
                    notifier.delete_discord_message(
                        "discord_main",
                        message_id,
                    )
                except Exception:
                    pass

            raise

        state_dir = Path(
            "state"
        )

        state_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        state_file = (
            state_dir
            / "weekly_calendar.json"
        )

        state_data = {
            "message_ids": message_ids,
            "updated_at": (
                self.malaysia_now()
                .isoformat()
            ),
        }

        state_file.write_text(
            json.dumps(
                state_data,
                indent=2,
            ),
            encoding="utf-8",
        )

        self.console.success(
            "Weekly calendar sent to "
            f"Discord in "
            f"{len(message_ids)} "
            f"message part(s)."
        )

    def update_discord_message(
        self,
        messages: list[str] | str,
        alert_options,
    ) -> None:

        from .alerts.notifiers import (
            NotifierFactory,
        )

        if isinstance(
            messages,
            str,
        ):
            messages = [messages]

        state_file = (
            Path("state")
            / "weekly_calendar.json"
        )

        if not state_file.exists():
            self.console.step(
                "No weekly calendar state "
                "file found. "
                "Nothing to update."
            )
            return

        try:
            state_data = json.loads(
                state_file.read_text(
                    encoding="utf-8"
                )
            )

        except (
            json.JSONDecodeError,
            OSError,
        ):
            self.console.step(
                "Weekly calendar state "
                "file could not be read."
            )
            return

        # New multi-message state format.
        existing_ids = (
            state_data.get(
                "message_ids",
                [],
            )
        )

        # Backward compatibility with
        # the old single-message format.
        if not existing_ids:

            legacy_message_id = (
                state_data.get(
                    "message_id"
                )
            )

            if legacy_message_id:
                existing_ids = [
                    str(
                        legacy_message_id
                    )
                ]

        existing_ids = [
            str(message_id)
            for message_id
            in existing_ids
            if message_id
        ]

        if not existing_ids:
            self.console.step(
                "No weekly calendar "
                "message IDs found. "
                "Nothing to update."
            )
            return

        notifier = NotifierFactory(
            alert_options
        )

        updated_ids: list[str] = []

        for index, message in enumerate(
            messages
        ):

            if index < len(
                existing_ids
            ):
                message_id = (
                    existing_ids[index]
                )

                notifier.edit_discord_message(
                    "discord_main",
                    message_id,
                    message,
                )

                updated_ids.append(
                    message_id
                )

            else:
                new_message_id = (
                    notifier.send_raw(
                        "discord_main",
                        message,
                    )
                )

                if not new_message_id:
                    raise RuntimeError(
                        "Discord did not return "
                        "a message ID for new "
                        "weekly calendar part."
                    )

                updated_ids.append(
                    str(new_message_id)
                )

        # Delete extra current-week parts
        # if fewer parts are now required.
        if (
            len(existing_ids)
            > len(messages)
        ):

            extra_ids = (
                existing_ids[
                    len(messages):
                ]
            )

            for message_id in extra_ids:

                notifier.delete_discord_message(
                    "discord_main",
                    message_id,
                )

        state_data.pop(
            "message_id",
            None,
        )

        state_data["message_ids"] = (
            updated_ids
        )

        state_data["updated_at"] = (
            self.malaysia_now()
            .isoformat()
        )

        state_file.write_text(
            json.dumps(
                state_data,
                indent=2,
            ),
            encoding="utf-8",
        )

        self.console.success(
            "Weekly calendar Discord "
            f"message updated "
            f"({len(updated_ids)} "
            f"part(s))"
        )

    def run(
        self,
        target_timezone: str,
        allowed_currencies: list[str],
        allowed_impacts: list[str],
        alert_options,
    ) -> int:

        monday, friday = (
            self.get_week_dates()
        )

        month_selectors = (
            self.get_month_selectors(
                monday,
                friday,
            )
        )

        records = self.scrape_week(
            monday,
            friday,
            month_selectors,
            target_timezone,
            allowed_currencies,
            allowed_impacts,
        )

        messages = (
            self.format_calendar(
                records,
                monday,
                friday,
            )
        )

        self.send_to_discord(
            messages,
            alert_options,
        )

        return 0