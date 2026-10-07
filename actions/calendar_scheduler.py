from core.user_paths import get_user_data_dir
# actions/calendar_scheduler.py
"""
Calendar and Schedule Management for Brahma AI.

Allows creating, listing, checking, and managing calendar appointments,
meetings, and events with local persistent storage and .ics calendar exports.
"""

import json
import os
import re
import sys
import uuid
import threading
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EVENTS_FILE = get_user_data_dir() / "memory" / "calendar_events.json"
_EVENTS_LOCK = threading.RLock()
_MAX_EVENTS = 5000
_MAX_TEXT = 5000

PLUGIN = {
    "name": "calendar_scheduler",
    "description": (
        "Manages calendar events, meetings, and schedules. Supports actions: "
        "'add_event', 'list_events', 'check_day', 'delete_event', 'get_upcoming', 'export_ics'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: add_event, list_events, check_day, delete_event, get_upcoming, export_ics",
            },
            "title": {
                "type": "STRING",
                "description": "Title or summary of the meeting/event.",
            },
            "date": {
                "type": "STRING",
                "description": "Date of event (YYYY-MM-DD or 'today', 'tomorrow').",
            },
            "time": {
                "type": "STRING",
                "description": "Time of event (HH:MM in 24h format, e.g. '14:30' or '3:00 PM').",
            },
            "duration_minutes": {
                "type": "NUMBER",
                "description": "Duration in minutes (default: 30).",
            },
            "location": {
                "type": "STRING",
                "description": "Location or online meeting URL (optional).",
            },
            "description": {
                "type": "STRING",
                "description": "Additional notes or agenda (optional).",
            },
            "event_id": {
                "type": "STRING",
                "description": "ID of event to delete (optional).",
            },
        },
        "required": ["action"],
    },
}


def _safe_events_text() -> str:
    if os.name == "nt":
        from core.windows_file_safety import read_text
        text, _size = read_text(EVENTS_FILE, max_chars=4 * 1024 * 1024)
        return text
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(EVENTS_FILE, flags)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read(4 * 1024 * 1024 + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def _load_events() -> list[dict]:
    with _EVENTS_LOCK:
        if not EVENTS_FILE.exists():
            return []
        try:
            raw = _safe_events_text()
            if len(raw.encode("utf-8")) > 4 * 1024 * 1024:
                raise ValueError("Calendar event store exceeds the 4 MiB safety limit.")
            data = json.loads(raw)
            if not isinstance(data, list) or len(data) > _MAX_EVENTS:
                raise ValueError("Calendar event store is malformed.")
            return [event for event in data if isinstance(event, dict)]
        except Exception as exc:
            print(f"[Calendar] Load error: {exc}")
            raise RuntimeError("Calendar data is corrupt; refusing to replace it with an empty calendar.") from exc


def _save_events(events: list[dict]) -> None:
    with _EVENTS_LOCK:
        if len(events) > _MAX_EVENTS:
            raise ValueError("Calendar contains too many events.")
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = EVENTS_FILE.with_name(f".{EVENTS_FILE.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(events, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temp.replace(EVENTS_FILE)


def _ics_escape(value: object) -> str:
    return (
        str(value or "")[:_MAX_TEXT]
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _parse_date(date_str: str | None) -> str:
    """Normalizes natural date strings to YYYY-MM-DD."""
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")

    d = date_str.lower().strip()
    now = datetime.now()

    if d in ("today", "bugün"):
        return now.strftime("%Y-%m-%d")
    elif d in ("tomorrow", "yarın"):
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    elif d in ("yesterday", "dün"):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # Try standard formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    raise ValueError(f"Invalid calendar date: {date_str!r}")


def _parse_time(time_str: str | None) -> str:
    """Normalizes time string to HH:MM and rejects malformed input."""
    if not time_str:
        return datetime.now().strftime("%H:%M")

    t = str(time_str).strip().upper()
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%H.%M"):
        try:
            return datetime.strptime(t, fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise ValueError(f"Invalid calendar time: {time_str!r}")


def calendar_scheduler(
    parameters: dict,
    response: str | None = None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """
    Main entry point for calendar operations.
    """
    p = parameters if isinstance(parameters, dict) else {}
    action_raw = p.get("action", "list_events")
    action = str(action_raw or "list_events").strip().lower()
    title = str(p.get("title", "") or "").strip()[:_MAX_TEXT]
    try:
        date_str = _parse_date(p.get("date"))
        time_str = _parse_time(p.get("time"))
    except ValueError as exc:
        return str(exc)
    try:
        duration = int(p.get("duration_minutes") or 30)
    except (TypeError, ValueError):
        return "Duration must be a whole number of minutes."
    if duration < 1 or duration > 10080:
        return "Duration must be between 1 minute and 7 days."
    location = str(p.get("location", "") or "").strip()[:_MAX_TEXT]
    desc = str(p.get("description", "") or "").strip()[:_MAX_TEXT]
    event_id = str(p.get("event_id", "") or "").strip()

    events = _load_events()

    if action in ("add", "add_event", "create", "new"):
        if not title:
            return "Please provide a title or name for the calendar event."

        new_event = {
            "id": str(uuid.uuid4())[:8],
            "title": title,
            "date": date_str,
            "time": time_str,
            "duration_minutes": duration,
            "location": location,
            "description": desc,
            "created_at": datetime.now().isoformat(),
        }
        if len(events) >= _MAX_EVENTS:
            return "Calendar event limit reached; please remove older events first."
        events.append(new_event)
        # Keep sorted by date and time
        events.sort(key=lambda x: (x.get("date", ""), x.get("time", "")))
        _save_events(events)

        if player:
            try:
                player.write_log(f"[Calendar] Scheduled: '{title}' on {date_str} at {time_str}")
            except Exception:
                pass

        return f"Event '{title}' scheduled for {date_str} at {time_str} ({duration} mins)."

    elif action in ("list", "list_events", "upcoming", "get_upcoming"):
        now_date = datetime.now().strftime("%Y-%m-%d")
        upcoming = [e for e in events if e.get("date", "") >= now_date]

        if not upcoming:
            return "You have no upcoming events on your calendar."

        lines = [f"📅 Upcoming Events ({len(upcoming)}):"]
        for ev in upcoming[:8]:
            loc_str = f" @ {ev['location']}" if ev.get("location") else ""
            lines.append(f"• {ev.get('date')} {ev.get('time')}: {ev.get('title')}{loc_str} (ID: {ev.get('id')})")

        return "\n".join(lines)

    elif action in ("check_day", "today", "tomorrow"):
        target_date = date_str
        day_events = [e for e in events if e.get("date") == target_date]

        if not day_events:
            return f"No events scheduled for {target_date}."

        lines = [f"📅 Schedule for {target_date}:"]
        for ev in day_events:
            loc_str = f" [{ev['location']}]" if ev.get("location") else ""
            lines.append(f"• {ev.get('time')} - {ev.get('title')}{loc_str}")

        return "\n".join(lines)

    elif action in ("delete", "delete_event", "remove", "cancel"):
        if not event_id and not title:
            return "Please specify the event title or ID to delete."

        orig_count = len(events)
        if event_id:
            events = [e for e in events if e.get("id") != event_id]
        elif title:
            events = [e for e in events if title.lower() not in e.get("title", "").lower()]

        if len(events) < orig_count:
            _save_events(events)
            return "Event removed from calendar."
        else:
            return "Could not find a matching event to remove."

    elif action in ("export_ics", "export"):
        ics_lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Brahma AI//Calendar Scheduler//EN",
        ]
        for ev in events:
            try:
                dt_start = datetime.strptime(f"{ev['date']} {ev['time']}", "%Y-%m-%d %H:%M")
                duration_value = int(ev.get("duration_minutes", 30))
                if duration_value < 1 or duration_value > 10080:
                    raise ValueError("duration out of range")
                dt_end = dt_start + timedelta(minutes=duration_value)
                ics_lines.extend([
                    "BEGIN:VEVENT",
                    f"UID:{ev.get('id', uuid.uuid4())}@brahma.ai",
                    f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
                    f"DTSTART:{dt_start.strftime('%Y%m%dT%H%M%S')}",
                    f"DTEND:{dt_end.strftime('%Y%m%dT%H%M%S')}",
                    f"SUMMARY:{_ics_escape(ev.get('title'))}",
                    f"DESCRIPTION:{_ics_escape(ev.get('description', ''))}",
                    f"LOCATION:{_ics_escape(ev.get('location', ''))}",
                    "END:VEVENT",
                ])
            except Exception as exc:
                raise RuntimeError(
                    f"Calendar export aborted because event '{ev.get('id', 'unknown')}' is malformed."
                ) from exc
        ics_lines.append("END:VCALENDAR")

        desktop_ics = Path.home() / "Desktop" / "brahma_calendar.ics"
        temp_ics = desktop_ics.with_name(f".{desktop_ics.name}.{uuid.uuid4().hex}.tmp")
        temp_ics.write_text("\n".join(ics_lines), encoding="utf-8")
        temp_ics.replace(desktop_ics)
        return f"Exported calendar events to {desktop_ics}"

    return f"Unknown calendar action: '{action}'."


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin wrapper for Brahma architecture."""
    return calendar_scheduler(parameters, player=player, session_memory=session_memory)
