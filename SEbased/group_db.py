# group_db.py
import json
import logging
import os

logger = logging.getLogger(__name__)
DB_FILE = 'group_database.json'

def load_db():
    """Loads the group database from the JSON file."""
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading group database: {e}. Returning empty DB.")
        return {}

def save_db(db_data):
    """Saves the group database to the JSON file."""
    try:
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(db_data, f, indent=4, ensure_ascii=False)
    except IOError as e:
        logger.error(f"Could not save group database: {e}")