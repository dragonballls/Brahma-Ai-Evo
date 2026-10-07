from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_calendar_corruption_does_not_silently_reset_user_data():
    source = (ROOT / "actions" / "calendar_scheduler.py").read_text(encoding="utf-8")
    assert "Calendar data is corrupt; refusing to replace it with an empty calendar." in source
    assert "raise RuntimeError" in source


def test_calendar_writes_are_atomic_and_serialized():
    source = (ROOT / "actions" / "calendar_scheduler.py").read_text(encoding="utf-8")
    assert "_EVENTS_LOCK = threading.RLock()" in source
    assert "temp.replace(EVENTS_FILE)" in source
    assert "uuid.uuid4().hex" in source


def test_calendar_duration_and_event_count_are_bounded():
    source = (ROOT / "actions" / "calendar_scheduler.py").read_text(encoding="utf-8")
    assert "duration < 1 or duration > 10080" in source
    assert "_MAX_EVENTS = 5000" in source
    assert "Calendar event limit reached" in source


def test_calendar_ics_fields_are_escaped():
    source = (ROOT / "actions" / "calendar_scheduler.py").read_text(encoding="utf-8")
    assert "def _ics_escape" in source
    assert '_ics_escape(ev.get("title"))' in source
    assert '_ics_escape(ev.get("description", \'\'))' in source
    assert '_ics_escape(ev.get("location", \'\'))' in source


def test_calendar_title_is_bounded_and_export_is_atomic():
    source = (ROOT / "actions" / "calendar_scheduler.py").read_text(encoding="utf-8")
    assert 'title = str(p.get("title", "") or "").strip()[:_MAX_TEXT]' in source
    assert 'temp_ics = desktop_ics.with_name' in source
    assert 'temp_ics.replace(desktop_ics)' in source


def test_calendar_rejects_malformed_date_and_time(monkeypatch, tmp_path):
    import actions.calendar_scheduler as calendar

    monkeypatch.setattr(calendar, "EVENTS_FILE", tmp_path / "calendar_events.json")

    assert "Invalid calendar date" in calendar.calendar_scheduler(
        {"action": "add_event", "title": "Test", "date": "not-a-date", "time": "12:00"}
    )
    assert "Invalid calendar time" in calendar.calendar_scheduler(
        {"action": "add_event", "title": "Test", "date": "2035-01-02", "time": "25:99"}
    )


def test_calendar_rejects_non_mapping_parameters_without_crashing():
    import actions.calendar_scheduler as calendar

    result = calendar.calendar_scheduler(["not", "a", "mapping"])
    assert result == "You have no upcoming events on your calendar."
