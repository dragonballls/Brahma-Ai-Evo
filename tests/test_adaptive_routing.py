def test_routing_uses_learned_model_score(tmp_path, monkeypatch):
    from core import model_performance as perf
    from core import intelligence_orchestrator as orch

    monkeypatch.setattr(perf, "_PATH", tmp_path / "models.json")
    perf.record(provider="openai", model="openai/weak", profile="coding", score=50)
    perf.record(provider="openai", model="openai/strong", profile="coding", score=100)

    result = orch._select_provider_model(
        "openai",
        ("openai/weak", "openai/strong"),
        {},
        "coding",
    )
    assert result == "openai/strong"
