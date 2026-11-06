"""Small transport-only time headers; canonical Task titles remain lossless."""

from datetime import UTC
from zoneinfo import ZoneInfo

from docket.models import Task

TITLE_PREFIX_KEY = "transport_title_prefix"


def task_time_header(task: Task) -> tuple[str, bool]:
    if task.remind_at is None or not task.reminder_timezone:
        return "", False
    local = task.remind_at.replace(tzinfo=task.remind_at.tzinfo or UTC).astimezone(
        ZoneInfo(task.reminder_timezone),
    )
    label = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    if task.scheduled_date is not None and task.scheduled_date != local.date():
        label = local.date().isoformat() + " " + label
    prefix = label + " — "
    if task.title.startswith(prefix):
        # Preserve an operator-authored prefix as ordinary canonical title text.
        return "", False
    if len(prefix + task.title) > 1024:
        return "", True
    return prefix, False
