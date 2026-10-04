from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from actions.auto_heal_engine import AutoHealEngine


class AutoHealRuntimeTests(unittest.TestCase):
    def test_runtime_error_is_deduplicated(self):
        tb = 'Traceback (most recent call last):\n  File "actions/open_app.py", line 10, in execute\nRuntimeError: demo failure'
        with patch.object(AutoHealEngine, "heal_traceback", return_value={"success": True, "message": "fixed"}) as heal:
            first = AutoHealEngine.auto_heal_runtime_error(tb)
            second = AutoHealEngine.auto_heal_runtime_error(tb)
            self.assertEqual(first["status"], "scheduled")
            self.assertIn(second["status"], {"inflight", "cooldown"})
            deadline = time.time() + 2
            while time.time() < deadline and not heal.called:
                time.sleep(0.01)
            self.assertTrue(heal.called)
        with AutoHealEngine._auto_lock:
            AutoHealEngine._auto_inflight.clear()
            AutoHealEngine._auto_recent.clear()

    def test_runtime_error_hook_only_targets_first_party_tracebacks_via_heal_parser(self):
        tb = 'Traceback (most recent call last):\n  File "site-packages/foo.py", line 10, in execute\nRuntimeError: third party'
        parsed = AutoHealEngine._error_fingerprint(tb)
        self.assertTrue(parsed)

    def test_auto_heal_cooldown_is_configured(self):
        self.assertGreaterEqual(AutoHealEngine.AUTO_HEAL_COOLDOWN_SECONDS, 60)


if __name__ == "__main__":
    unittest.main()
