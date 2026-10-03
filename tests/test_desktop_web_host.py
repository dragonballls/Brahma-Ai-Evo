from __future__ import annotations

from unittest import TestCase


class WebHostNormalizationTests(TestCase):
    def test_url_normalization_without_qt(self):
        from core.desktop.web_host import normalize_web_url
        self.assertEqual(normalize_web_url("https://google.com"), "https://google.com")
        self.assertEqual(normalize_web_url("google.com"), "https://google.com")
        self.assertEqual(normalize_web_url("www.google.com"), "https://www.google.com")
        self.assertEqual(normalize_web_url("file:///C:/Brahma/index.html"), "file:///C:/Brahma/index.html")

    def test_qt_window_helper_uses_same_normalizer(self):
        from core.desktop.web_host import WebApplicationWindow
        self.assertEqual(
            WebApplicationWindow._normalize_url("example.com"),
            "https://example.com",
        )
