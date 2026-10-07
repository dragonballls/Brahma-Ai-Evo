
def test_supervisor_test_mode_requires_ready_marker(monkeypatch, tmp_path):
    import core.process_supervisor as supervisor

    class Guard:
        def acquire(self): return True
        def release(self): pass

    class Process:
        pid = 1
        returncode = 0
        def wait(self, timeout=None): return 0

    monkeypatch.setattr(supervisor, "SingleInstance", lambda _name: Guard())
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(supervisor, "_base_dir", lambda: tmp_path)
    monkeypatch.setattr(supervisor.Path, "exists", lambda self: False)

    monkeypatch.setenv("BRAHMA_EVO_TEST_MODE", "1")
    monkeypatch.setenv("BRAHMA_EVO_TEST_SUPERVISOR_TIMEOUT", "10")
    assert supervisor.supervise(tmp_path, max_cycles=1) == 1
