# group_db.py
import json
import logging
import os
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)
DB_FILE = 'group_database.json'

_db_lock = threading.Lock()


@contextmanager
def _locked_db():
    """Context manager that acquires the database lock."""
    _db_lock.acquire()
    try:
        yield
    finally:
        _db_lock.release()


def load_db():
    """Loads the group database from the JSON file in a thread-safe manner."""
    with _locked_db():
        if not os.path.exists(DB_FILE):
            return {}
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error loading group database: {e}. Returning empty DB.")
            return {}


def save_db(db_data):
    """Saves the group database to the JSON file in a thread-safe manner."""
    with _locked_db():
        try:
            with open(DB_FILE, 'w', encoding='utf-8') as f:
                json.dump(db_data, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Could not save group database: {e}")