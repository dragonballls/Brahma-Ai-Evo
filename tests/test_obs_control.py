import base64
import hashlib
import unittest

from actions import obs_control


class TestOBSControl(unittest.TestCase):
    def test_obs_v5_auth_formula(self):
        password = "secret"
        salt = "salt"
        challenge = "challenge"
        secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
        expected = base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")
        self.assertEqual(obs_control._auth(password, salt, challenge), expected)

    def test_source_lookup_exact_then_fuzzy(self):
        items = [{"sourceName": "Face Cam", "sceneItemId": 4}, {"sourceName": "Game Capture", "sceneItemId": 5}]
        self.assertEqual(obs_control._find_item(items, "FACE CAM")["sceneItemId"], 4)
        self.assertEqual(obs_control._find_item(items, "capture")["sceneItemId"], 5)

    def test_preset_position_accounts_for_center_alignment(self):
        transform = {"canvasWidth": 1920, "canvasHeight": 1080, "width": 400, "height": 300, "alignment": 0}
        x, y = obs_control._preset_position("top-right", transform)
        self.assertEqual(x, 1692.8)
        self.assertEqual(y, 37.8)

    def test_preset_position_rejects_unknown_name(self):
        with self.assertRaises(obs_control.OBSControlError):
            obs_control._preset_position("somewhere", {})


if __name__ == "__main__":
    unittest.main()
