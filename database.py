# database.py
import sqlite3
import threading

DATABASE_NAME = "bot_data.db"
_db_lock = threading.Lock()

def init_db():
    with _db_lock:
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                solana_private_key TEXT,
                solana_address TEXT
            )
        """)
        conn.commit()
        conn.close()

def set_user_wallet(user_id: int, private_key: str, public_address: str):
    with _db_lock:
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_wallets (user_id, solana_private_key, solana_address)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                solana_private_key = excluded.solana_private_key,
                solana_address = excluded.solana_address
        """, (user_id, private_key, public_address))
        conn.commit()
        conn.close()

def get_user_wallet(user_id: int):
    with _db_lock:
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT solana_private_key, solana_address
            FROM user_wallets WHERE user_id = ?
        """, (user_id,))
        result = cursor.fetchone()
        conn.close()
        if result:
            return {
                "private_key": result[0],
                "address": result[1]
            }
        return {"private_key": None, "address": None}

def delete_user_wallet(user_id: int):
    with _db_lock:
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_wallets SET
                solana_private_key = NULL,
                solana_address = NULL
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()
        conn.close()

init_db()