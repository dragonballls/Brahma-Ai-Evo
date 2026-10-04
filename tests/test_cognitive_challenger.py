from core.cognitive_challenger import CASES, SUITE_ID, compare, run


def test_suite_is_versioned_and_has_multiple_domains():
    assert SUITE_ID.startswith("brahma-astra-challenger-")
    assert len(CASES) >= 8
    assert len({case.dimension for case in CASES}) >= 6


def test_run_scores_deterministic_cases(tmp_path, monkeypatch):
    import core.cognitive_challenger as challenger
    monkeypatch.setattr(challenger, "RESULTS_PATH", tmp_path / "results.json")
    answers = {
        "What is the smallest positive integer divisible by every integer from 1 through 10?": "2520",
        "A box contains 3 red, 4 blue, and 5 green balls. What is the minimum number "
        "drawn without looking that guarantees two balls of the same color?": "4",
    }

    def solver(prompt, _dimension):
        return answers.get(prompt, "evidence logs reproduce rollback")

    result = run(
        solver,
        system="brahma",
        cases=CASES[:4],
        persist_intellect=False,
    )
    assert result["case_count"] == 4
    assert result["mean_score"] >= 75


def test_compare_requires_same_suite():
    try:
        compare({"suite_id": "x", "rows": []}, {"suite_id": SUITE_ID, "rows": []})
    except ValueError:
        pass
    else:
        raise AssertionError("different suites must not be compared")


def test_compare_same_cases():
    rows_a = [{"case_id": "x", "score": 90}]
    rows_b = [{"case_id": "x", "score": 80}]
    result = compare(
        {"suite_id": SUITE_ID, "rows": rows_a},
        {"suite_id": SUITE_ID, "rows": rows_b},
    )
    assert result["comparison"] == "brahma-beats-astra"
    assert result["mean_delta"] == 10
