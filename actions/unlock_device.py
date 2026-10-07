# actions/unlock_device.py
"""
Deprecated compatibility shim.

Remote PIN unlock is intentionally not exposed by Brahma Connect because the
Android agent does not provide a safe, supported lock-screen unlock primitive.
This module is retained only so older direct imports fail closed without
sending credentials or commands.
"""

def unlock_device(parameters: dict, response=None, player=None, session_memory=None) -> str:
    return "Remote PIN unlock is not supported; no unlock operation was performed."
