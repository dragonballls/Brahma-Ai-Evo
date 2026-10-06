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
