def test_model_performance_is_bounded(tmp_path, monkeypatch):
    from core import model_performance as perf
    monkeypatch.setattr(perf, "_PATH", tmp_path / "models.json")
    perf.record(provider="openai", model="openai/test", profile="coding", score=100, latency_ms=10)
    assert 0 <= perf.routing_bonus(provider="openai", model="openai/test", profile="coding") <= 6


def test_unknown_model_has_neutral_bonus(tmp_path, monkeypatch):
    from core import model_performance as perf
    monkeypatch.setattr(perf, "_PATH", tmp_path / "models.json")
    assert perf.routing_bonus(provider="x", model="x/test", profile="smart") == 0.0

def test_model_performance_uses_atomic_replacement():
    from core import model_performance as perf
    import inspect

    source = inspect.getsource(perf._save)
    assert ".tmp" in source
    assert ".replace(" in source



def test_corrupt_model_performance_state_is_quarantined_and_not_reset(tmp_path, monkeypatch):
    from core import model_performance as perf
    import pytest

    path = tmp_path / "models.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(perf, "_PATH", path)

    with pytest.raises(RuntimeError, match="corrupt"):
        perf.lookup(provider="x", model="y", profile="z")

    assert not path.exists()
    assert len(list(tmp_path.glob("models.json.corrupt-*"))) == 1
