"""Compatibility facade for the canonical Instagram integration.

The active implementation lives in :mod:`actions.instagram_mcp`. This module
keeps the historical import surface without maintaining a second daemon,
credential loader, or polling loop.
"""
from __future__ import annotations

from typing import Any


def _service():
    from actions.instagram_mcp import InstagramService
    return InstagramService.instance()


def set_ig_prompt_callback(callback):
    return _service().set_prompt_callback(callback)


def add_auto_thread(thread_id):
    return _service().add_auto_thread(thread_id)


def send_direct_reply(thread_id, text):
    return _service().send_dm(thread_id, text, open_in_browser=True)


def get_recent_messages(amount: int = 5) -> str:
    return _service().get_recent_messages(amount)


def start_daemon():
    return _service().start_daemon()


def stop_daemon():
    return _service().stop_daemon()
