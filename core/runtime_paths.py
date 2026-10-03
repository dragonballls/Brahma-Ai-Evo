"""Single source of truth for Brahma Evo runtime-owned paths and repository identity."""
from __future__ import annotations

from pathlib import Path

from core.user_paths import get_user_data_dir


USER_DATA_DIR = get_user_data_dir()
CONFIG_DIR = USER_DATA_DIR / "config"
LOG_DIR = USER_DATA_DIR / "logs"

API_CONFIG_PATH = CONFIG_DIR / "api_keys.json"
APP_SETTINGS_PATH = CONFIG_DIR / "app_settings.json"
IDENTITY_PATH = CONFIG_DIR / "identity.json"
DISCORD_SETTINGS_PATH = CONFIG_DIR / "discord_bot.json"
STARTUP_LOG_PATH = LOG_DIR / "startup.log"
FATAL_CRASH_LOG_PATH = LOG_DIR / "FATAL_CRASH.log"
PATCH_HISTORY_PATH = CONFIG_DIR / "patch_history.json"
PATCH_BACKUPS_DIR = CONFIG_DIR / "patch_backups"

GITHUB_OWNER = "dragonballls"
GITHUB_REPOSITORY = "Brahma-Ai-Evo"
GITHUB_BRANCH = "main"
GITHUB_REMOTE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}.git"

OMNIROUTE_DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1"
