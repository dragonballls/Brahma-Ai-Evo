from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_learned_rules_use_atomic_lock_protected_storage():
    source = (ROOT / "core" / "learned_rules.py").read_text(encoding="utf-8")
    assert "_lock = threading.RLock()" in source
    assert "os.replace(temp, RULES_FILE)" in source
    assert 'open(RULES_FILE, "w"' not in source


def test_learned_rules_reject_credential_like_values():
    source = (ROOT / "core" / "learned_rules.py").read_text(encoding="utf-8")
    assert "_SECRET_RE" in source
    assert "_TOKEN_RE" in source
    assert "Credential-like values cannot be stored" in source



def test_learned_rules_reject_link_like_target(tmp_path, monkeypatch):
    from core import learned_rules

    target = tmp_path / "real.json"
    target.write_text("[]", encoding="utf-8")
    link = tmp_path / "learned_rules.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        return

    monkeypatch.setattr(learned_rules, "RULES_FILE", link)
    monkeypatch.setattr(learned_rules, "CONFIG_DIR", tmp_path)
    assert learned_rules.LearnedRulesEngine._save_raw([]) is False


def test_learned_rules_use_exclusive_temp_and_final_state_verification(tmp_path, monkeypatch):
    from core import learned_rules

    target = tmp_path / "learned_rules.json"
    monkeypatch.setattr(learned_rules, "RULES_FILE", target)
    monkeypatch.setattr(learned_rules, "CONFIG_DIR", tmp_path)

    assert learned_rules.LearnedRulesEngine._save_raw([{"id": "1"}]) is True
    assert target.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".learned_rules.json.*.tmp"))
