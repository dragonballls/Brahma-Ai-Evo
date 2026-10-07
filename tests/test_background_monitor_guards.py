from actions import background_monitor


def test_background_monitor_clamps_nonpositive_intervals(monkeypatch):
    background_monitor._monitors.clear()
    monkeypatch.setattr(background_monitor, "_ensure_monitor_thread", lambda: None)
    background_monitor.add_monitor("system", "cpu", 80, interval_sec=0)
    monitor_id = next(iter(background_monitor._monitors))
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

def test_background_monitor_rejects_private_website_targets(monkeypatch):
    monkeypatch.setattr(
        background_monitor.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))],
    )
    monkeypatch.setattr(background_monitor, "_ensure_monitor_thread", lambda: None)
    try:
        background_monitor.add_monitor("website", "http://example.test", 1)
        raise AssertionError("private website target was accepted")
    except ValueError as exc:
        assert "private" in str(exc).lower() or "local" in str(exc).lower()


def test_background_monitor_rejects_credentialed_and_malformed_crypto_targets(monkeypatch):
    monkeypatch.setattr(background_monitor, "_ensure_monitor_thread", lambda: None)
    for monitor_type, target in (
        ("website", "https://user:pass@example.com/"),
        ("crypto", "bitcoin&foo=bar"),
    ):
        try:
            background_monitor.add_monitor(monitor_type, target, 1)
            raise AssertionError("unsafe monitor target was accepted")
        except ValueError:
            pass


def test_background_monitor_clamps_large_intervals_and_limits_count(monkeypatch):
    background_monitor._monitors.clear()
    monkeypatch.setattr(background_monitor, "_ensure_monitor_thread", lambda: None)
    background_monitor.add_monitor("system", "cpu", 80, interval_sec=999999)
    monitor_id = next(iter(background_monitor._monitors))
    try:
        assert background_monitor._monitors[monitor_id]["interval"] == 86400
        background_monitor._monitors.clear()
        for index in range(background_monitor.MAX_MONITORS):
            background_monitor._monitors[str(index)] = {
                "type": "system", "target": "cpu", "threshold": 80,
                "condition": "above", "interval": 1, "last_check": 0,
            }
        try:
            background_monitor.add_monitor("system", "cpu", 80)
            raise AssertionError("monitor limit was not enforced")
        except RuntimeError:
            pass
    finally:
        background_monitor._monitors.clear()
