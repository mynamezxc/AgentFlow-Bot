from datetime import datetime, timedelta, timezone
from .store import DomainError

VN = timezone(timedelta(hours=7))


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def next_schedule(schedule, after=None):
    current = (after or datetime.now(timezone.utc)).astimezone(VN)
    if schedule.get("mode") == "interval":
        minutes = int(schedule.get("interval_minutes", 60))
        if not 5 <= minutes <= 10080:
            raise DomainError("The interval must be between 5 minutes and 7 days.")
        return (current + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()
    try:
        hour, minute = map(int, schedule.get("time", "08:30").split(":"))
        following = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (ValueError, TypeError):
        raise DomainError("Run time must use the HH:MM format.") from None
    if following <= current:
        following += timedelta(days=1)
    return following.astimezone(timezone.utc).isoformat()
