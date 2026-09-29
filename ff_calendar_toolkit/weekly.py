from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .console import AppConsole
from .normalize import normalize_rows
from .scraper import ForexFactoryScraper


MALAYSIA_TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")


class WeeklyCalendarService:
    def __init__(self, console: AppConsole | None = None) -> None:
        self.console = console or AppConsole()

    def malaysia_now(self) -> datetime:
        return datetime.now(MALAYSIA_TIMEZONE)

    def get_week_dates(self) -> tuple[date, date]:
        today = self.malaysia_now().date()
        weekday = today.weekday()

        if weekday == 6:
            # Sunday → upcoming Monday to Friday
            monday = today + timedelta(days=1)

        elif weekday == 5:
            # Saturday → upcoming Monday to Friday
            monday = today + timedelta(days=2)

        else:
            # Monday to Friday → current Monday to Friday
            monday = today - timedelta(days=weekday)

        friday = monday + timedelta(days=4)

        return monday, friday

    def get_month_selectors(
        self,
        monday: date,
        friday: date,
    ) -> list[str]:
        selectors = []

        if monday.month == friday.month:
            today = self.malaysia_now().date()

            if (
                monday.month == today.month
                and monday.year == today.year
            ):
                selectors.append("this")
            else:
                selectors.append("next")

            return selectors

        selectors.append("this")
        selectors.append("next")

        return selectors

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

        weekly_records = []

        for month in month_selectors:
            self.console.step(
                f"Scraping month selector '{month}'"
            )

            raw_rows, context = scraper.scrape_month(
                month,
                target_timezone,
            )

            self.console.step(
                f"Normalizing {len(raw_rows)} raw rows for "
                f"{context.month_name} {context.year}"
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
                except (ValueError, TypeError):
                    continue

                if monday <= event_date <= friday:
                    weekly_records.append(record)

        weekly_records.sort(
            key=lambda record: (
                datetime.strptime(
                    record["date"],
                    "%d/%m/%Y",
                ),
                record.get("time", ""),
            )
        )

        return weekly_records

    def format_calendar(
        self,
        records: list[dict],
        monday: date,
        friday: date,
    ) -> str:
        lines = [
            "📅 WEEKLY ECONOMIC CALENDAR",
            f"{monday.strftime('%d %b')} – "
            f"{friday.strftime('%d %b %Y')}",
            "",
        ]

        current_date = None

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

        for record in records:
            event_date = datetime.strptime(
                record["date"],
                "%d/%m/%Y",
            ).date()

            if event_date != current_date:
                current_date = event_date

                lines.extend(
                    [
                        "",
                        f"{event_date.strftime('%A').upper()}",
                        "",
                    ]
                )

            currency = record.get("currency", "")
            flag = currency_flags.get(currency, "")

            impact = impact_icons.get(
                record.get("impact", ""),
                "⚪",
            )

            lines.append(
                f"{record.get('time', '')} MYT — "
                f"{flag} {currency} — "
                f"{record.get('event', '')} {impact}"
            )

            actual = record.get("actual", "").strip()
            forecast = record.get("forecast", "").strip()
            previous = record.get("previous", "").strip()

            if actual:
                lines.append(
                    f"   Actual: {actual}"
                )

            if forecast:
                lines.append(
                    f"   Forecast: {forecast}"
                )

            if previous:
                lines.append(
                    f"   Previous: {previous}"
                )

        if not records:
            lines.append(
                "No high or medium impact events found."
            )

        return "\n".join(lines)

    def send_to_discord(
        self,
        message: str,
        alert_options,
    ) -> None:
        from .alerts.notifiers import NotifierFactory
        import json
        from pathlib import Path

        notifier = NotifierFactory(alert_options)

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
            state_dir / "weekly_calendar.json"
        )

        state_data = {
            "message_id": message_id,
            "updated_at": self.malaysia_now().isoformat(),
        }

        state_file.write_text(
            json.dumps(
                state_data,
                indent=2,
            ),
            encoding="utf-8",
        )

        self.console.success(
            f"Weekly calendar sent to Discord. "
            f"Message ID: {message_id}"
        )

    def update_discord_message(
        self,
        message: str,
        alert_options,
    ) -> None:
        import json
        from pathlib import Path

        state_file = (
            Path("state") / "weekly_calendar.json"
        )

        if not state_file.exists():
            self.console.step(
                "No weekly calendar state file found. "
                "Nothing to update."
            )
            return

        state_data = json.loads(
            state_file.read_text(
                encoding="utf-8"
            )
        )

        message_id = state_data.get("message_id")

        if not message_id:
            self.console.step(
                "No weekly calendar message ID found. "
                "Nothing to update."
            )
            return

        from .alerts.notifiers import NotifierFactory

        notifier = NotifierFactory(alert_options)

        notifier.edit_discord_message(
            "discord_main",
            str(message_id),
            message,
        )

        state_data["updated_at"] = (
            self.malaysia_now().isoformat()
        )

        state_file.write_text(
            json.dumps(
                state_data,
                indent=2,
            ),
            encoding="utf-8",
        )

        self.console.success(
            "Weekly calendar Discord message updated"
        )

    def run(
        self,
        target_timezone: str,
        allowed_currencies: list[str],
        allowed_impacts: list[str],
        alert_options,
    ) -> int:
        monday, friday = self.get_week_dates()

        month_selectors = self.get_month_selectors(
            monday,
            friday,
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