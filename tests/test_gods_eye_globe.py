from __future__ import annotations

import sys
import unittest
from pathlib import Path
import tempfile
import json

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.gods_eye import GeoPoint, LocationSnapshot, _point
from core.gods_eye_globe import GlobeLocator, build_globe_payload


class GodsEyeGlobeContractTests(unittest.TestCase):
    def test_geo_point_bounds(self):
        self.assertEqual(GeoPoint(34.1, -117.7).longitude, -117.7)
        with self.assertRaises(ValueError):
            GeoPoint(91, 0)

    def test_globe_locator_serializes(self):
        item = GlobeLocator("current", "Current location", 10, 20, "current", True, 25, "test")
        payload = item.as_dict()
        self.assertEqual(payload["latitude"], 10)
        self.assertEqual(payload["kind"], "current")

    def test_globe_payload_current_and_saved(self):
        class FakeEye:
            def _current_snapshot(self):
                return LocationSnapshot(GeoPoint(10, 20), 25, True, "test")

            def provider_locations(self):
                return []

            def saved_locations(self):
                return [{
                    "name": "Home",
                    "point": {"latitude": 30, "longitude": 40},
                    "source": "saved",
                }]

        payload = build_globe_payload(FakeEye())
        self.assertTrue(payload["authorized_current"])
        self.assertEqual(len(payload["locators"]), 2)
        self.assertEqual(payload["locators"][0]["kind"], "current")
        self.assertEqual(payload["locators"][1]["kind"], "saved")

    def test_unpermitted_current_is_hidden(self):
        class FakeEye:
            def _current_snapshot(self):
                return LocationSnapshot(GeoPoint(10, 20), None, False, "permission-denied")

            def provider_locations(self):
                return []

            def saved_locations(self):
                return []

        self.assertEqual(build_globe_payload(FakeEye())["locators"], [])

    def test_metadata_location_shape(self):
        from core.gods_eye import GodsEye
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app_settings.json"
            path.write_text(json.dumps({
                "saved_locations": [{
                    "name": "Home",
                    "latitude": 1,
                    "longitude": 2,
                }]
            }), encoding="utf-8")
            eye = GodsEye(Path(tmp))
            self.assertEqual(eye.saved_locations()[0]["point"]["latitude"], 1)

    def test_point_rejects_invalid_coordinates(self):
        self.assertIsNone(_point({"latitude": 100, "longitude": 0}))


if (require := None) is None:
    pass


if __name__ == "__main__":
    unittest.main()
