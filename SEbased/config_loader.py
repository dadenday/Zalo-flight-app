# config_loader.py

import json
import logging

logger = logging.getLogger(__name__)

def load_main_config():
    """Loads the main config.json file."""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        # Filter for only enabled sites
        config['sites'] = [s for s in config.get('sites', []) if s.get('enabled', False)]
        logger.info("Configuration loaded successfully")
        return config
    except FileNotFoundError:
        logger.error("FATAL: config.json not found. Please create it.")
        exit()
    except Exception as e:
        logger.error(f"Could not load or parse config.json: {e}")
        exit()

def load_user_settings():
    """Loads user-defined group settings from settings.json."""
    try:
        with open('settings.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Return default structure if file doesn't exist or is empty
        return {"control_group": "", "monitoring_groups": [], "stay_group": ""}

def save_user_settings(settings_to_save):
    """Saves user-defined group settings to settings.json."""
    try:
        with open('settings.json', 'w', encoding='utf-8') as f:
            json.dump(settings_to_save, f, indent=4)
        logger.info("User settings saved to settings.json")
    except Exception as e:
        logger.error(f"Could not save group settings: {e}")

def load_timestamps(all_group_titles):
    """Loads the last message check timestamps from timestamps.json."""
    from datetime import datetime, timedelta
    try:
        with open('timestamps.json', 'r') as f:
            timestamps_str = json.load(f)
        
        loaded_stamps = {group: datetime.fromisoformat(ts) for group, ts in timestamps_str.items()}
        logger.info("Loaded timestamps from previous session")
        
        # Ensure all monitored groups have a timestamp entry
        for group in all_group_titles:
            if group not in loaded_stamps:
                loaded_stamps[group] = datetime.now() - timedelta(minutes=10)
        return loaded_stamps
        
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("No valid timestamp file found. Starting fresh for all groups.")
        # Default to 10 minutes ago to catch recent messages on first run
        return {title: datetime.now() - timedelta(minutes=10) for title in all_group_titles}
    except Exception as e:
        logger.error(f"Could not load timestamps: {e}")
        return {title: datetime.now() - timedelta(minutes=10) for title in all_group_titles}


def save_timestamps(last_check_times):
    """Saves the last message check timestamps to timestamps.json."""
    timestamps_str = {group: ts.isoformat() for group, ts in last_check_times.items()}
    try:
        with open('timestamps.json', 'w') as f:
            json.dump(timestamps_str, f, indent=4)
        logger.debug("Timestamps saved successfully.")
    except Exception as e:
        logger.error(f"Could not save timestamps: {e}")