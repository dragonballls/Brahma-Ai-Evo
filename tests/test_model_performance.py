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


def test_model_performance_rejects_nonfinite_samples(tmp_path, monkeypatch):
    from core import model_performance as perf
    import math
    import pytest

    monkeypatch.setattr(perf, "_PATH", tmp_path / "models.json")
    with pytest.raises(ValueError, match="finite"):
        perf.record(provider="x", model="x/test", profile="smart", score=math.nan)
    with pytest.raises(ValueError, match="finite"):
        perf.record(provider="x", model="x/test", profile="smart", score=50, latency_ms=math.inf)


def test_model_performance_ignores_poisoned_persisted_numeric_state(tmp_path, monkeypatch):
    from core import model_performance as perf

    path = tmp_path / "models.json"
    path.write_text(
        '{"schema_version":1,"models":{"x|x/test|smart":'
        '{"provider":"x","model":"x/test","profile":"smart","samples":1,'
        '"mean_score":NaN,"mean_latency_ms":0}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(perf, "_PATH", path)
    assert perf.routing_bonus(provider="x", model="x/test", profile="smart") == 0.0
