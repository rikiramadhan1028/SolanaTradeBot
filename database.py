# file: database.py
import os, time
from typing import Optional, Dict, Any

from pymongo import MongoClient, ASCENDING
from cryptography.fernet import Fernet
from base64 import urlsafe_b64encode
from hashlib import sha256
from secrets import token_bytes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB  = os.getenv("MONGO_DB", "soltrade")
FERNET_KEY = os.getenv("FERNET_KEY")  # base64 urlsafe 32 bytes (Fernet)

if not MONGO_URI:
    raise RuntimeError("MONGO_URI missing")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY missing")

client = MongoClient(MONGO_URI, appname="RokuTrade")
db = client[MONGO_DB]
wallets = db["wallets"]
wallets.create_index([("user_id", ASCENDING)], unique=True)

# ------- crypto helpers -------
_app_fernet = Fernet(FERNET_KEY.encode())

def _derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    # 32-byte key via PBKDF2-HMAC-SHA256
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

def _enc_with_app_key(plaintext: str) -> Dict[str, Any]:
    token = _app_fernet.encrypt(plaintext.encode())
    return {"v": 1, "enc": token.decode()}

def _dec_with_app_key(data: Dict[str, Any]) -> str:
    return _app_fernet.decrypt(data["enc"].encode()).decode()

def _enc_with_user_pass(plaintext: str, passphrase: str) -> Dict[str, Any]:
    salt = token_bytes(16)
    k = _derive_key_from_passphrase(passphrase, salt)
    f = Fernet(k)
    token = f.encrypt(plaintext.encode())
    return {"v": 2, "salt": salt.hex(), "enc": token.decode()}

def _dec_with_user_pass(data: Dict[str, Any], passphrase: str) -> str:
    salt = bytes.fromhex(data["salt"])
    k = _derive_key_from_passphrase(passphrase, salt)
    f = Fernet(k)
    return f.decrypt(data["enc"].encode()).decode()

# ------- public API -------

def get_user_wallet(user_id: int) -> Dict[str, Any]:
    """Return doc without exposing secret."""
    doc = wallets.find_one({"user_id": int(user_id)}) or {}
    if not doc:
        return {}
    # redact
    return {
        "user_id": doc["user_id"],
        "address": doc.get("address"),
        "has_secret": bool(doc.get("pk")),
        "enc_v": (doc.get("pk") or {}).get("v"),
        "has_passphrase": (doc.get("pk") or {}).get("v") == 2,
        "updated_at": doc.get("updated_at"),
    }

def set_user_wallet(user_id: int, private_key_plain: str, address: str, passphrase: Optional[str] = None) -> None:
    """Create/replace wallet for user; encrypt secret."""
    user_id = int(user_id)
    if passphrase:
        pk = _enc_with_user_pass(private_key_plain, passphrase)
    else:
        pk = _enc_with_app_key(private_key_plain)
    wallets.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "address": address,
            "pk": pk,                     # encrypted secret
            "addr_hash": sha256(address.encode()).hexdigest(),
            "updated_at": int(time.time()),
        }},
        upsert=True,
    )

def migrate_plain_to_encrypted() -> int:
    """
    One-time migration if any doc still has 'private_key' plaintext -> move to pk(v=1).
    Returns number of migrated docs.
    """
    cnt = 0
    for doc in wallets.find({"private_key": {"$exists": True}}):
        try:
            pk_plain = doc["private_key"]
            pk = _enc_with_app_key(pk_plain)
            wallets.update_one(
                {"_id": doc["_id"]},
                {"$set": {"pk": pk, "updated_at": int(time.time())}, "$unset": {"private_key": ""}}
            )
            cnt += 1
        except Exception:
            continue
    return cnt

def get_private_key_decrypted(user_id: int, passphrase: Optional[str] = None) -> Optional[str]:
    """
    Decrypt secret in memory; returns None if wallet not found or passphrase required & not provided/wrong.
    """
    doc = wallets.find_one({"user_id": int(user_id)})
    if not doc or "pk" not in doc:
        return None
    pk = doc["pk"]
    v = pk.get("v")
    try:
        if v == 1:
            return _dec_with_app_key(pk)
        elif v == 2:
            if not passphrase:
                return None
            return _dec_with_user_pass(pk, passphrase)
    except Exception:
        return None
    return None

def upgrade_to_passphrase(user_id: int, passphrase: str) -> bool:
    """Re-encrypt existing secret with user passphrase."""
    user_id = int(user_id)
    doc = wallets.find_one({"user_id": user_id})
    if not doc or "pk" not in doc:
        return False
    # decrypt with whatever it currently uses
    cur_plain = get_private_key_decrypted(user_id, None)
    if cur_plain is None:
        return False
    new_pk = _enc_with_user_pass(cur_plain, passphrase)
    wallets.update_one({"user_id": user_id}, {"$set": {"pk": new_pk, "updated_at": int(time.time())}})
    return True

def remove_wallet(user_id: int) -> None:
    wallets.delete_one({"user_id": int(user_id)})
