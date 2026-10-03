from __future__ import annotations

from unittest import TestCase


class WebHostNormalizationTests(TestCase):
    def test_url_normalization(self):
        # Keep this test independent of Qt/WebEngine so CI can validate the
        # deterministic part even when GUI dependencies are unavailable.
        from core.desktop.web_host import WebApplicationWindow
        self.assertEqual(
            WebApplicationWindow._normalize_url("https://google.com"),
            "https://google.com",
        )
        self.assertEqual(
            WebApplicationWindow._normalize_url("google.com"),
            "https://google.com",
        )
