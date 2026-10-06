from actions import background_monitor


def test_background_monitor_clamps_nonpositive_intervals(monkeypatch):
    background_monitor._monitors.clear()
    monkeypatch.setattr(background_monitor, "_ensure_monitor_thread", lambda: None)
    monitor_id = background_monitor.add_monitor("system", "cpu", 80, interval_sec=0)
    try:
        assert background_monitor._monitors[monitor_id]["interval"] == 1
    finally:
        background_monitor._monitors.pop(monitor_id, None)


def test_background_monitor_rejects_unsupported_type_and_condition(monkeypatch):
    monkeypatch.setattr(background_monitor, "_ensure_monitor_thread", lambda: None)
    try:
        background_monitor.add_monitor("unknown", "cpu", 80)
        raise AssertionError("unsupported monitor type was accepted")
    except ValueError:
        pass
    try:
        background_monitor.add_monitor("system", "cpu", 80, condition="sideways")
        raise AssertionError("unsupported monitor condition was accepted")
    except ValueError:
        pass
