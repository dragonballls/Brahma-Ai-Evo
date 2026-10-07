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
