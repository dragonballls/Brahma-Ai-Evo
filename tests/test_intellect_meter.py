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


def test_index_uses_brahma_rows_only(tmp_path, monkeypatch):
    monkeypatch.setattr(intellect_meter, "_DATA_PATH", tmp_path / "intellect.json")
    intellect_meter.record_benchmark(
        suite="reasoning-smoke",
        dimension="reasoning",
        score=90,
        system="brahma",
        evidence="verified",
    )
    intellect_meter.record_benchmark(
        suite="reasoning-smoke",
        dimension="reasoning",
        score=99,
        system="gpt-6-astra",
        evidence="external baseline",
    )
    state = intellect_meter.snapshot()
    assert state.score is not None
    assert round(state.score, 1) == 90.0


def test_astra_comparison_requires_same_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(intellect_meter, "_DATA_PATH", tmp_path / "intellect.json")
    intellect_meter.record_benchmark(
        suite="suite-a",
        dimension="coding",
        score=80,
        system="brahma",
    )
    intellect_meter.record_benchmark(
        suite="suite-b",
        dimension="coding",
        score=100,
        system="gpt-6-astra",
    )
    assert intellect_meter.snapshot().astra_comparison == "not established"


def test_intellect_answer_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(intellect_meter, "_DATA_PATH", tmp_path / "intellect.json")
    answer = intellect_meter.answer_intellect_query("tell me your intelligence level")
    assert answer is not None
    assert answer == intellect_meter.format_status()
