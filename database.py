# file: database.py
import os, time
from typing import Optional, Dict, Any

from pymongo import MongoClient, ASCENDING
from cryptography.fernet import Fernet, InvalidToken
from base64 import urlsafe_b64encode
from hashlib import sha256
from secrets import token_bytes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# ----------------- ENV -----------------
MONGO_URI  = os.getenv("MONGO_URI")
MONGO_DB   = os.getenv("MONGO_DB", "soltrade")
FERNET_KEY = os.getenv("FERNET_KEY")  # base64 urlsafe 32-byte key (may end with '=')

if not MONGO_URI:
    raise RuntimeError("MONGO_URI missing")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY missing")

# ----------------- Mongo -----------------
client  = MongoClient(MONGO_URI, appname="RokuTrade")
db      = client[MONGO_DB]
wallets = db["wallets"]
wallets.create_index([("user_id", ASCENDING)], unique=True)

# ----------------- Crypto helpers -----------------
_app_fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

def _derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    """Derive 32-byte key via PBKDF2-HMAC-SHA256, output as base64 urlsafe for Fernet."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

def _enc_with_app_key(plaintext: str) -> Dict[str, Any]:
    token = _app_fernet.encrypt(plaintext.encode())
    return {"v": 1, "enc": token.decode()}

def _dec_with_app_key(data: Dict[str, Any]) -> Optional[str]:
    try:
        return _app_fernet.decrypt(data["enc"].encode()).decode()
    except Exception:
        return None

def _enc_with_user_pass(plaintext: str, passphrase: str) -> Dict[str, Any]:
    salt  = token_bytes(16)
    k     = _derive_key_from_passphrase(passphrase, salt)
    f     = Fernet(k)
    token = f.encrypt(plaintext.encode())
    return {"v": 2, "salt": salt.hex(), "enc": token.decode()}

def _dec_with_user_pass(data: Dict[str, Any], passphrase: Optional[str]) -> Optional[str]:
    if not passphrase:
        return None
    try:
        salt = bytes.fromhex(data["salt"])
        k    = _derive_key_from_passphrase(passphrase, salt)
        f    = Fernet(k)
        return f.decrypt(data["enc"].encode()).decode()
    except (InvalidToken, Exception):
        return None

# ----------------- Public API -----------------
def set_user_wallet(
    user_id: int,
    private_key_plain: str,
    address: str,
    passphrase: Optional[str] = None,
) -> None:
    """
    Simpan/replace wallet user dengan enkripsi at-rest.
    Default: v=1 (app Fernet). Jika passphrase diberikan → v=2.
    """
    user_id = int(user_id)
    pk_obj = _enc_with_user_pass(private_key_plain, passphrase) if passphrase else _enc_with_app_key(private_key_plain)

    wallets.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "address": address,
            "pk": pk_obj,                          # ONLY encrypted secret
            "addr_hash": sha256(address.encode()).hexdigest(),
            "updated_at": int(time.time()),
        }},
        upsert=True,
    )

def get_user_wallet(user_id: int, passphrase: Optional[str] = None) -> Dict[str, Any]:
    """
    Balikkan shape yang dipakai code lain:
    {
      "user_id": int,
      "address": str|None,
      "private_key": str|None,   # didekripsi in-memory; None kalau gagal
      "locked": bool,            # True bila ada pk tapi gagal decrypt (key mismatch / butuh passphrase)
      "has_passphrase": bool,    # pk v2
    }
    """
    doc = wallets.find_one({"user_id": int(user_id)}) or {}
    if not doc:
        return {"user_id": int(user_id), "address": None, "private_key": None, "locked": False, "has_passphrase": False}

    # Transparan migrasi lama (jika masih ada plaintext field "private_key")
    if "private_key" in doc and "pk" not in doc:
        try:
            pk_plain = doc["private_key"]
            pk_obj   = _enc_with_app_key(pk_plain)
            wallets.update_one({"_id": doc["_id"]}, {"$set": {"pk": pk_obj}, "$unset": {"private_key": ""}})
            doc["pk"] = pk_obj
        except Exception:
            pass

    pk = doc.get("pk")
    priv = None
    locked = False
    has_pass = False

    if isinstance(pk, dict):
        v = pk.get("v")
        if v == 1:
            priv = _dec_with_app_key(pk)
            locked = (priv is None)
        elif v == 2:
            has_pass = True
            priv = _dec_with_user_pass(pk, passphrase)
            locked = (priv is None)
        else:
            locked = True

    return {
        "user_id": int(doc.get("user_id")),
        "address": doc.get("address"),
        "private_key": priv,       # None if not decryptable
        "locked": bool(locked),
        "has_passphrase": bool(has_pass),
    }

def get_private_key_decrypted(user_id: int, passphrase: Optional[str] = None) -> Optional[str]:
    """Convenience: hanya private key didekripsi atau None."""
    w = get_user_wallet(user_id, passphrase=passphrase)
    return w.get("private_key")

def upgrade_to_passphrase(user_id: int, passphrase: str) -> bool:
    """Re-encrypt pk dari v1→v2 menggunakan passphrase user."""
    user_id = int(user_id)
    doc = wallets.find_one({"user_id": user_id})
    if not doc or "pk" not in doc:
        return False
    # decrypt dengan scheme saat ini (tanpa passphrase, karena awalnya v1)
    current_plain = get_private_key_decrypted(user_id)
    if current_plain is None:
        return False
    new_pk = _enc_with_user_pass(current_plain, passphrase)
    wallets.update_one({"user_id": user_id}, {"$set": {"pk": new_pk, "updated_at": int(time.time())}})
    return True

def migrate_plain_to_encrypted() -> int:
    """
    Sapu bersih doc lama yang masih menyimpan 'private_key' plaintext → pindah ke pk(v=1).
    Kembalikan jumlah dokumen yang dimigrasi.
    """
    cnt = 0
    for doc in wallets.find({"private_key": {"$exists": True}}):
        try:
            pk_plain = doc["private_key"]
            pk_obj   = _enc_with_app_key(pk_plain)
            wallets.update_one(
                {"_id": doc["_id"]},
                {"$set": {"pk": pk_obj, "updated_at": int(time.time())}, "$unset": {"private_key": ""}},
            )
            cnt += 1
        except Exception:
            continue
    return cnt

def delete_user_wallet(user_id: int) -> None:
    """Alias lama 'remove_wallet'."""
    wallets.delete_one({"user_id": int(user_id)})

# Backward-compatible name, if other modules still import this:
remove_wallet = delete_user_wallet

# ==== Copy Trading collections ====
# collection: copy_follows
# doc: {
#   user_id: int,
#   leader_address: str,
#   ratio: float,                # 1.0 = 100%
#   max_sol_per_trade: float,    # cap buy per trade
#   slippage_bps: int|None,
#   follow_buys: bool,
#   follow_sells: bool,
#   active: bool,
#   created_at: int,
# }
copy_follows = db["copy_follows"]
copy_follows.create_index([("user_id", ASCENDING), ("leader_address", ASCENDING)], unique=True)

# collection: copy_leaders (hanya untuk daftar leader yang ada minimal 1 follower aktif)
# doc: { leader_address: str, active: bool }
copy_leaders = db["copy_leaders"]
copy_leaders.create_index([("leader_address", ASCENDING)], unique=True)

def copy_follow_upsert(user_id: int, leader_address: str, *,
                       ratio: float = 1.0,
                       max_sol_per_trade: float = 0.5,
                       slippage_bps: int | None = None,
                       follow_buys: bool = True,
                       follow_sells: bool = True,
                       active: bool = True) -> None:
    now = int(time.time())
    copy_follows.update_one(
        {"user_id": int(user_id), "leader_address": leader_address},
        {"$set": {
            "user_id": int(user_id),
            "leader_address": leader_address,
            "ratio": float(ratio),
            "max_sol_per_trade": float(max_sol_per_trade),
            "slippage_bps": int(slippage_bps) if slippage_bps is not None else None,
            "follow_buys": bool(follow_buys),
            "follow_sells": bool(follow_sells),
            "active": bool(active),
            "created_at": now,
        }},
        upsert=True,
    )
    # ensure leader record exists & active
    copy_leaders.update_one(
        {"leader_address": leader_address},
        {"$set": {"leader_address": leader_address, "active": True}},
        upsert=True,
    )

def copy_follow_remove(user_id: int, leader_address: str) -> None:
    copy_follows.delete_one({"user_id": int(user_id), "leader_address": leader_address})
    # if no more followers, optionally deactivate leader
    if copy_follows.count_documents({"leader_address": leader_address}) == 0:
        copy_leaders.update_one({"leader_address": leader_address}, {"$set": {"active": False}})

def copy_follow_list_for_user(user_id: int) -> list[dict]:
    return list(copy_follows.find({"user_id": int(user_id)}))

def copy_follow_list_for_leader(leader_address: str) -> list[dict]:
    return list(copy_follows.find({"leader_address": leader_address, "active": True}))

def copy_leaders_active() -> list[dict]:
    return list(copy_leaders.find({"active": True}))

# ===== Positions (per user x token) =====
# doc shape (contoh field yang kita pakai sekarang):
# {
#   user_id, mint,
#   buy_count, sell_count,
#   buy_sol, sell_sol, buy_tokens, sell_tokens,
#   avg_entry_price_usd,            # weighted by tokens
#   avg_entry_mc_usd,               # optional, weighted by tokens
#   updated_at
# }
positions_collection = db["positions"]
positions_collection.create_index([("user_id", ASCENDING), ("mint", ASCENDING)], unique=True)

def position_get(user_id: int, mint: str):
    return positions_collection.find_one({"user_id": int(user_id), "mint": mint})

def position_upsert(doc: dict):
    doc = dict(doc)
    doc["user_id"] = int(doc["user_id"])
    doc["updated_at"] = int(time.time())
    positions_collection.update_one(
        {"user_id": doc["user_id"], "mint": doc["mint"]},
        {"$set": doc},
        upsert=True,
    )

def position_list(user_id: int):
    return list(positions_collection.find({"user_id": int(user_id)}))

# ===== User Settings (per user preferences) =====
# doc shape:
# {
#   user_id: int,
#   cu_price: int|None,          # compute unit price (micro-lamports per CU)
#   priority_tier: str|None,     # "fast", "turbo", "ultra", "custom", or None
#   updated_at: int,
# }
user_settings_collection = db["user_settings"]
user_settings_collection.create_index([("user_id", ASCENDING)], unique=True)

def user_settings_get(user_id: int) -> dict:
    """Get user settings document or empty dict if not found."""
    doc = user_settings_collection.find_one({"user_id": int(user_id)})
    return doc if doc else {}

def user_settings_upsert(user_id: int, cu_price: int = None, priority_tier: str = None) -> None:
    """Update or insert user settings."""
    doc = {
        "user_id": int(user_id),
        "updated_at": int(time.time())
    }
    
    # Only set fields that are provided (allow None to clear values)
    if cu_price is not None:
        doc["cu_price"] = int(cu_price)
    elif cu_price is None:
        doc["cu_price"] = None
        
    if priority_tier is not None:
        doc["priority_tier"] = str(priority_tier) if priority_tier else None
    elif priority_tier is None:
        doc["priority_tier"] = None

    user_settings_collection.update_one(
        {"user_id": int(user_id)},
        {"$set": doc},
        upsert=True,
    )

def user_settings_get_cu_price(user_id: int) -> int:
    """Get user's CU price setting or None."""
    doc = user_settings_get(user_id)
    return doc.get("cu_price")

def user_settings_set_cu_price(user_id: int, cu_price: int = None) -> None:
    """Set user's CU price setting."""
    user_settings_collection.update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "user_id": int(user_id),
            "cu_price": int(cu_price) if cu_price is not None else None,
            "updated_at": int(time.time())
        }},
        upsert=True,
    )

def user_settings_get_priority_tier(user_id: int) -> str:
    """Get user's priority tier setting or None."""
    doc = user_settings_get(user_id)
    return doc.get("priority_tier")

def user_settings_set_priority_tier(user_id: int, priority_tier: str = None) -> None:
    """Set user's priority tier setting."""
    user_settings_collection.update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "user_id": int(user_id),
            "priority_tier": str(priority_tier) if priority_tier else None,
            "updated_at": int(time.time())
        }},
        upsert=True,
    )

def user_settings_remove(user_id: int) -> None:
    """Remove all settings for a user."""
    user_settings_collection.delete_one({"user_id": int(user_id)})

def user_settings_list_all() -> list:
    """Get list of all users with settings."""
    return list(user_settings_collection.find())

def user_settings_count() -> int:
    """Get count of users with settings."""
    return user_settings_collection.count_documents({})
