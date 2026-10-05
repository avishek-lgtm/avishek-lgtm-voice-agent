"""Appointment availability from the weekly schedule in config/agent.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class BookingConfig:
    enabled: bool = True
    appointment_type: str = "free site visit"
    timezone: str = "Asia/Kolkata"
    duration_minutes: int = 60
    days_ahead: int = 14
    min_notice_hours: int = 24
    slots_to_offer: int = 8
    max_per_slot: int = 1
    # e.g. {"mon": ["10:00-13:00", "14:00-18:00"], "sun": []}
    weekly_hours: dict[str, list[str]] = field(default_factory=dict)
    closed_dates: list[str] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Slot:
    id: str          # short label the model refers to, e.g. "S3"
    start: datetime  # timezone-aware

    @property
    def iso(self) -> str:
        return self.start.isoformat(timespec="minutes")

    def spoken(self) -> str:
        """e.g. 'Tuesday 7 October at 10 am'"""
        hour = self.start.strftime("%I").lstrip("0")
        minute = "" if self.start.minute == 0 else self.start.strftime(":%M")
        ampm = self.start.strftime("%p").lower()
        return f"{self.start.strftime('%A')} {self.start.day} {self.start.strftime('%B')} at {hour}{minute} {ampm}"


def _parse_range(text: str) -> tuple[time, time]:
    start, end = (part.strip() for part in text.split("-"))
    return time.fromisoformat(start), time.fromisoformat(end)


def all_slots(cfg: BookingConfig, now: datetime | None = None) -> list[datetime]:
    """Every slot start in the booking window, ignoring existing bookings."""
    tz = cfg.tz
    now = (now or datetime.now(tz)).astimezone(tz)
    earliest = now + timedelta(hours=cfg.min_notice_hours)
    closed = {date.fromisoformat(d) for d in cfg.closed_dates}
    step = timedelta(minutes=cfg.duration_minutes)

    starts: list[datetime] = []
    for offset in range(cfg.days_ahead + 1):
        day = (now + timedelta(days=offset)).date()
        if day in closed:
            continue
        for block in cfg.weekly_hours.get(DAYS[day.weekday()], []) or []:
            open_t, close_t = _parse_range(block)
            t = datetime.combine(day, open_t, tz)
            end = datetime.combine(day, close_t, tz)
            while t + step <= end:
                if t >= earliest:
                    starts.append(t)
                t += step
    return starts


def available_slots(
    cfg: BookingConfig, booked_counts: dict[str, int], now: datetime | None = None
) -> list[Slot]:
    """Open slots, soonest first, limited to how many the agent should offer."""
    free = [
        s for s in all_slots(cfg, now)
        if booked_counts.get(s.isoformat(timespec="minutes"), 0) < cfg.max_per_slot
    ]
    # Spread the offer across days so the caller hears more than one morning.
    picked: list[datetime] = []
    per_day: dict[date, int] = {}
    per_day_cap = max(2, cfg.slots_to_offer // 4)
    for s in free:
        if len(picked) >= cfg.slots_to_offer:
            break
        if per_day.get(s.date(), 0) < per_day_cap:
            picked.append(s)
            per_day[s.date()] = per_day.get(s.date(), 0) + 1
    return [Slot(id=f"S{i + 1}", start=s) for i, s in enumerate(picked)]
