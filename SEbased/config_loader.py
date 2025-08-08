# config_loader.py

"""Utilities for loading and saving configuration with typed models."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class MainConfig:
    """Typed model for the main configuration."""

    sites: List[Dict[str, Any]] = field(default_factory=list)
    google_api_key: str = ""
    settings: Dict[str, Any] = field(default_factory=dict)
    zalo_selectors: Dict[str, Any] = field(default_factory=dict)
    vietjet_cargo: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UserSettings:
    """Typed model for user-defined group settings."""

    control_group: str = ""
    monitoring_groups: List[str] = field(default_factory=list)
    stay_group: str = ""


def load_main_config() -> MainConfig:
    """Loads the main config.json file into a :class:`MainConfig`."""

    try:
        with open("config.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        raw["sites"] = [s for s in raw.get("sites", []) if s.get("enabled", False)]
        logger.info("Configuration loaded successfully")
        return MainConfig(**raw)
    except FileNotFoundError:
        logger.error("FATAL: config.json not found. Please create it.")
        raise SystemExit(1)
    except Exception as e:  # pragma: no cover - unforeseen parse errors
        logger.error(f"Could not load or parse config.json: {e}")
        raise SystemExit(1)


def load_user_settings() -> UserSettings:
    """Loads user-defined group settings from settings.json."""

    try:
        with open("settings.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return UserSettings(**data)
    except (FileNotFoundError, json.JSONDecodeError):
        return UserSettings()


def save_user_settings(settings_to_save: UserSettings) -> None:
    """Saves user-defined group settings to settings.json."""

    try:
        with open("settings.json", "w", encoding="utf-8") as f:
            json.dump(settings_to_save.__dict__, f, indent=4)
        logger.info("User settings saved to settings.json")
    except Exception as e:  # pragma: no cover - disk write errors
        logger.error(f"Could not save group settings: {e}")


def load_timestamps(all_group_titles):
    """Loads the last message check timestamps from timestamps.json."""

    from datetime import datetime, timedelta

    try:
        with open("timestamps.json", "r") as f:
            timestamps_str = json.load(f)

        loaded_stamps = {
            group: datetime.fromisoformat(ts) for group, ts in timestamps_str.items()
        }
        logger.info("Loaded timestamps from previous session")

        for group in all_group_titles:
            if group not in loaded_stamps:
                loaded_stamps[group] = datetime.now() - timedelta(minutes=10)
        return loaded_stamps
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("No valid timestamp file found. Starting fresh for all groups.")
        return {
            title: datetime.now() - timedelta(minutes=10) for title in all_group_titles
        }
    except Exception as e:  # pragma: no cover - unforeseen errors
        logger.error(f"Could not load timestamps: {e}")
        return {title: datetime.now() - timedelta(minutes=10) for title in all_group_titles}


def save_timestamps(last_check_times):
    """Saves the last message check timestamps to timestamps.json."""

    timestamps_str = {group: ts.isoformat() for group, ts in last_check_times.items()}
    try:
        with open("timestamps.json", "w") as f:
            json.dump(timestamps_str, f, indent=4)
        logger.debug("Timestamps saved successfully.")
    except Exception as e:  # pragma: no cover - disk write errors
        logger.error(f"Could not save timestamps: {e}")

