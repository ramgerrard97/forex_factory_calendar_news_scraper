from datetime import date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .console import AppConsole
from .normalize import normalize_rows
from .scraper import ForexFactoryScraper


MALAYSIA_TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")

# Discord allows up to 2000 characters.
# Keep a little safety margin.
DISCORD_SAFE_LIMIT = 1900


class WeeklyCalendarService:
    def __init__(
        self,
        console: AppConsole | None = None,
    ) -> None:
        self.console = console or AppConsole()

    def malaysia_now(self) -> datetime:
        return datetime.now(MALAYSIA_TIMEZONE)

    def get_week_dates(
        self,
    ) -> tuple[date, date]:
        today = self.malaysia_now().date()
        weekday = today.weekday()

        if weekday == 6:
            # Sunday -> upcoming Monday
            monday = today + timedelta(days=1)

        elif weekday == 5:
            # Saturday -> upcoming Monday
            monday = today + timedelta(days=2)

        else:
            # Monday-Friday -> current week's Monday
            monday = today - timedelta(
                days=weekday
            )

        friday = monday + timedelta(days=4)

        return monday, friday

    def get_month_selectors(
        self,
        monday: date,
        friday: date,
    ) -> list[str]:
        today = self.malaysia_now().date()

        # Entire week is inside one month.
        if (
            monday.month == friday.month
            and monday.year == friday.year
        ):
            if (
                monday.month == today.month
                and monday.year == today.year
            ):
                return ["this"]

            return ["next"]

        # Week crosses from current month
        # into the following month.
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
                f"Scraping month selector '{month}'"
            )

            raw_rows, context = (
                scraper.scrape_month(
                    month,
                    target_timezone,
                )
            )

            self.console.step(
                f"Normalizing {len(raw_rows)} "
                f"raw rows for "
                f"{context.month_name} "
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
                    event_date = datetime.strptime(
                        record["date"],
                        "%d/%m/%Y",
                    ).date()

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

    def format_calendar(
        self,
        records: list[dict],
        monday: date,
        friday: date,
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

        lines = [
            (
                "📅 **WEEKLY ECONOMIC CALENDAR**"
            ),
            (
                f"{monday.strftime('%d %b')} – "
                f"{friday.strftime('%d %b %Y')}"
            ),
        ]

        current_date = None

        for record in records:
            try:
                event_date = datetime.strptime(
                    record["date"],
                    "%d/%m/%Y",
                ).date()

            except (
                ValueError,
                TypeError,
                KeyError,
            ):
                continue

            if event_date != current_date:
                current_date = event_date

                lines.extend(
                    [
                        "",
                        (
                            f"**"
                            f"{event_date.strftime('%A').upper()}"
                            f"**"
                        ),
                    ]
                )

            currency = str(
                record.get(
                    "currency",
                    "",
                )
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
                ).strip(),
                "⚪",
            )

            event_name = str(
                record.get(
                    "event",
                    "",
                )
            ).strip()

            event_time = str(
                record.get(
                    "time",
                    "",
                )
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

            lines.append(event_line)

        if not records:
            lines.extend(
                [
                    "",
                    (
                        "No high or medium impact "
                        "events found."
                    ),
                ]
            )

        message = "\n".join(lines)

        if (
            len(message)
            <= DISCORD_SAFE_LIMIT
        ):
            return message

        # Never allow a Discord 2000-character
        # error to break the alert checker.
        warning = (
            "\n\n⚠️ More events exist this week, "
            "but the calendar was shortened to "
            "fit Discord's message limit."
        )

        available_length = (
            DISCORD_SAFE_LIMIT
            - len(warning)
        )

        safe_lines: list[str] = []

        for line in lines:
            candidate = "\n".join(
                safe_lines + [line]
            )

            if (
                len(candidate)
                > available_length
            ):
                break

            safe_lines.append(line)

        return (
            "\n".join(
                safe_lines
            ).rstrip()
            + warning
        )

    def send_to_discord(
        self,
        message: str,
        alert_options,
    ) -> None:
        from .alerts.notifiers import (
            NotifierFactory,
        )

        notifier = NotifierFactory(
            alert_options
        )

        message_id = notifier.send_raw(
            "discord_main",
            message,
        )

        state_dir = Path("state")

        state_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        state_file = (
            state_dir
            / "weekly_calendar.json"
        )

        state_data = {
            "message_id": message_id,
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
            "Discord. "
            f"Message ID: {message_id}"
        )

    def update_discord_message(
        self,
        message: str,
        alert_options,
    ) -> None:
        from .alerts.notifiers import (
            NotifierFactory,
        )

        state_file = (
            Path("state")
            / "weekly_calendar.json"
        )

        if not state_file.exists():
            self.console.step(
                "No weekly calendar state "
                "file found. Nothing to update."
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
                "Weekly calendar state file "
                "could not be read."
            )
            return

        message_id = state_data.get(
            "message_id"
        )

        if not message_id:
            self.console.step(
                "No weekly calendar message "
                "ID found. Nothing to update."
            )
            return

        notifier = NotifierFactory(
            alert_options
        )

        notifier.edit_discord_message(
            "discord_main",
            str(message_id),
            message,
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
            "message updated"
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

        message = self.format_calendar(
            records,
            monday,
            friday,
        )

        self.send_to_discord(
            message,
            alert_options,
        )

        return 0