from __future__ import annotations

import json
import time
from pathlib import Path


def test_crash_recovery_performs_real_rollback(monkeypatch, tmp_path):
    from core import crash_recovery

    base_dir = tmp_path / "repo"
    backups_dir = tmp_path / "backups"
    config_dir = tmp_path / "config"
    logs_dir = tmp_path / "logs"
    base_dir.mkdir()
    backups_dir.mkdir()
    config_dir.mkdir()
    logs_dir.mkdir()

    target = base_dir / "feature.py"
    backup = backups_dir / "feature.py"
    target.write_text("broken = True\n", encoding="utf-8")
    backup.write_text("broken = False\n", encoding="utf-8")

    crash_text = "synthetic packaged crash: ValueError\n"
    crash_log = logs_dir / "FATAL_CRASH.log"
    crash_log.write_text(crash_text, encoding="utf-8")

    crash_time = time.time()
    fingerprint = crash_recovery._fingerprint(crash_text)
    patch_id = "ci-recovery-smoke"
    history_path = config_dir / "patch_history.json"
    state_path = config_dir / "crash_recovery_state.json"
    recovery_log = logs_dir / "crash_recovery.log"

    history_path.write_text(
        json.dumps(
            [
                {
                    "patch_id": patch_id,
                    "status": "applied_by_crash_recovery",
                    "timestamp": crash_time - 1,
                    "target_file": str(target),
                    "backup_path": str(backup),
                }
            ]
        ),
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps(
            {
                "last_crash_fingerprint": fingerprint,
                "repair_attempts": 1,
                "blocked": False,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(crash_recovery, "BASE_DIR", base_dir)
    monkeypatch.setattr(crash_recovery, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(crash_recovery, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(crash_recovery, "FATAL_CRASH_LOG_PATH", crash_log)
    monkeypatch.setattr(crash_recovery, "PATCH_HISTORY_PATH", history_path)
    monkeypatch.setattr(crash_recovery, "CRASH_RECOVERY_STATE_PATH", state_path)
    monkeypatch.setattr(crash_recovery, "CRASH_RECOVERY_LOG_PATH", recovery_log)

    result = crash_recovery.recover_from_crash(crash_time=crash_time)

    assert result["success"] is True
    assert result["action"] == "rolled_back"
    assert target.read_text(encoding="utf-8") == "broken = False\n"

    saved_history = json.loads(history_path.read_text(encoding="utf-8"))
    assert saved_history[0]["status"] == "rolled_back_after_crash"
    saved_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved_state["blocked"] is True
    assert saved_state["last_action"] == "rolled_back"
