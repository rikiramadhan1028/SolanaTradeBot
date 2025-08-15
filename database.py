# file: database.py
"""
Wallet storage for the Telegram bot.

- If env MONGO_URL is present & reachable -> use MongoDB collection 'wallets'.
- Else -> fall back to a local JSON file at data/wallets.json.

API expected by the bot:
    get_user_wallet(user_id) -> {"private_key": str|None, "address": str|None}
    set_user_wallet(user_id, private_key, address) -> None
    delete_user_wallet(user_id) -> None
"""

from __future__ import annotations
import os
import json
import threading
from typing import Dict, Optional

# ---------- Mongo (optional) ----------
_MONGO_URL = os.getenv("MONGO_URL")
_col = None
if _MONGO_URL:
    try:
        from pymongo import MongoClient  # optional; falls back if not installed/reachable
        _client = MongoClient(_MONGO_URL, serverSelectionTimeoutMS=2000)
        _client.admin.command("ping")  # raises if not reachable

        # pick db from URL if present, otherwise default
        try:
            # if URL ends with /dbname use it; else default "soltrade"
            db_name = _client.get_database().name  # may raise if none in URL
            _db = _client[db_name]
        except Exception:
            _db = _client["soltrade"]

        _col = _db["wallets"]
        _col.create_index("user_id", unique=True)
        print("[database] Using MongoDB wallets collection.")
    except Exception as e:
        print(f"[database] MongoDB disabled (fallback to file): {e}")
        _col = None

# ---------- File fallback ----------
_LOCK = threading.Lock()
_DATA_DIR = os.getenv("DATA_DIR", "data")
_DATA_PATH = os.path.join(_DATA_DIR, "wallets.json")


def _ensure_file() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(_DATA_PATH):
        with open(_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f)


def _file_read() -> Dict[str, Dict[str, Optional[str]]]:
    _ensure_file()
    with open(_DATA_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}


def _file_write(store: Dict[str, Dict[str, Optional[str]]]) -> None:
    tmp = _DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DATA_PATH)


# ---------- Public API ----------
def get_user_wallet(user_id: int) -> Dict[str, Optional[str]]:
    """Return {"private_key": str|None, "address": str|None} for the given Telegram user_id."""
    uid = int(user_id)
    if _col is not None:
        doc = _col.find_one({"user_id": uid}, {"_id": 0, "private_key": 1, "address": 1})
        if doc:
            return {"private_key": doc.get("private_key"), "address": doc.get("address")}
        return {"private_key": None, "address": None}

    with _LOCK:
        store = _file_read()
        doc = store.get(str(uid)) or {}
        return {"private_key": doc.get("private_key"), "address": doc.get("address")}


def set_user_wallet(user_id: int, private_key: str, address: str) -> None:
    """Create or update wallet for user_id."""
    uid = int(user_id)
    if _col is not None:
        _col.update_one(
            {"user_id": uid},
            {"$set": {"private_key": private_key, "address": address}},
            upsert=True,
        )
        return

    with _LOCK:
        store = _file_read()
        store[str(uid)] = {"private_key": private_key, "address": address}
        _file_write(store)


def delete_user_wallet(user_id: int) -> None:
    """Delete wallet for user_id (no error if missing)."""
    uid = int(user_id)
    if _col is not None:
        _col.delete_one({"user_id": uid})
        return

    with _LOCK:
        store = _file_read()
        store.pop(str(uid), None)
        _file_write(store)
