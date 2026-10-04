from core import intellect_meter


def test_intellect_query_detection():
    assert intellect_meter.is_intellect_query("Jarvis, what is your intellect level?")
    assert intellect_meter.is_intellect_query("how smart is Brahma")
    assert not intellect_meter.is_intellect_query("fix my repository")


def test_unmeasured_state_never_invents_score(tmp_path, monkeypatch):
    monkeypatch.setattr(intellect_meter, "_DATA_PATH", tmp_path / "intellect.json")
    text = intellect_meter.format_status()
    assert "Verified intelligence score: UNMEASURED" in text
    assert "not established yet" in text
    assert "IQ score" in text


def test_index_uses_latest_measured_dimensions(tmp_path, monkeypatch):
    monkeypatch.setattr(intellect_meter, "_DATA_PATH", tmp_path / "intellect.json")
    intellect_meter.record_benchmark(
        suite="reasoning-smoke",
        dimension="reasoning",
        score=90,
        evidence="verified",
    )
    intellect_meter.record_benchmark(
        suite="coding-smoke",
        dimension="coding",
        score=80,
        evidence="verified",
    )
    state = intellect_meter.snapshot()
    assert state.score is not None
    assert 80 <= state.score <= 90
    assert state.coverage > 0
    assert "Reasoning" in state.strongest or "Coding" in state.strongest


def test_intellect_answer_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(intellect_meter, "_DATA_PATH", tmp_path / "intellect.json")
    answer = intellect_meter.answer_intellect_query("tell me your intelligence level")
    assert answer is not None
    assert answer == intellect_meter.format_status()
