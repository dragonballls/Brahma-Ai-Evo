"""Canonical runtime/build contract for Brahma Evo.

Keep versions and verified release pins here so Python, packaging, CI, and
Windows bootstrap logic cannot silently drift apart.
"""
from __future__ import annotations

PYTHON_MAJOR_MINOR = "3.12"
PYTHON_BOOTSTRAP_VERSION = "3.12.10"
NODE_VERSION = "24.21.0"
OMNIROUTE_VERSION = "3.8.50"
OMNIROUTE_COMMIT = "5458026c216f77a3da68ea49152dc33470cfe2cb"
