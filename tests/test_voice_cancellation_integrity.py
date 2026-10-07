from __future__ import annotations

import actions.attention_monitor as am


def test_stale_native_tts_generation_is_discarded(monkeypatch):
    with am._speech_generation_lock:
        am._speech_generation = 5

    called = {"sapi": False, "edge": False}

    monkeypatch.setattr(am, "_speak_sapi_male", lambda *args, **kwargs: called.__setitem__("sapi", True))

    class ForbiddenEdge:
        def __getattr__(self, name):
            called["edge"] = True
            raise AssertionError(f"stale speech imported edge_tts via {name}")

    monkeypatch.setitem(__import__("sys").modules, "edge_tts", ForbiddenEdge())

    # Generation 4 represents a worker that was queued before an interruption.
    am._speak_edge_native("stale", generation=4)

    assert called == {"sapi": False, "edge": False}


def test_stop_native_speech_invalidates_generation(monkeypatch):
    with am._speech_generation_lock:
        before = am._speech_generation
    monkeypatch.setattr(am, "_cleanup_current_audio", lambda: None)

    am.stop_native_speech()

    with am._speech_generation_lock:
        after = am._speech_generation
    assert after == before + 1


def test_edge_tts_generation_rechecks_cancellation_before_playback(monkeypatch, tmp_path):
    import sys
    import types

    audio_path = tmp_path / "stale.mp3"
    class Communicate:
        def __init__(self, *args, **kwargs):
            pass
        def save_sync(self, path):
            Path(path).write_bytes(b"audio")
            with am._speech_generation_lock:
                am._speech_generation += 1

    fake_edge = types.SimpleNamespace(Communicate=Communicate)
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    monkeypatch.setattr(am.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(am, "_current_speech_generation", lambda: 10)
    cleanup = {"called": False}
    monkeypatch.setattr(am, "_cleanup_current_audio", lambda: cleanup.__setitem__("called", True))
    # The generation passed into the worker is intentionally stale after
    # save_sync() simulates an interruption.
    am._speak_edge_native("stale", generation=9)
    assert not am._current_player_alias
    assert not am._current_audio_path
    assert not any(tmp_path.glob("brahma_edge_tts_*.mp3"))
